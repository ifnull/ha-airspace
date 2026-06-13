"""SQLite history journal — durable ``first_seen`` across restarts (Phase 2b).

The project's memory. Without it, ``first_seen`` is reset to "now" every time a
track is newly created — including the first sighting after a restart — so a
long-tracked aircraft loses its history on every restart. The journal persists
``first_seen`` once and restores it when the track reappears.

Design (DESIGN §2b):

* **stdlib ``sqlite3``** (no new dependency). DB work runs in a thread executor
  so the event loop never blocks on disk.
* **SD-card friendly.** WAL + ``synchronous=NORMAL``. Track-summary writes are
  *coalesced*: buffered in memory and flushed on a timer (``write_coalesce_s``)
  or once the buffer hits ``write_coalesce_events`` — never one write per poll.
* **Two retention policies.** ``track`` summary rows (``first_seen``) are kept
  **forever**; ``event`` rows (flag/alert transitions) are pruned after
  ``retention_observations_days``.
* **Warm-load at boot.** ``load_first_seen()`` reads the whole track table once
  into a dict so the per-track restore on a busy first poll is a dict lookup,
  not a DB hit per new track.
* **Fail-soft.** A locked/slow DB degrades to "history not saved," never a
  stalled poll loop — same posture as receivers and the DB loader.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ha_airspace.config import JournalConfig

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track (
    track_id   TEXT PRIMARY KEY,
    hex        TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    id        INTEGER PRIMARY KEY,
    track_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_at ON event(at);
CREATE INDEX IF NOT EXISTS event_track ON event(track_id);
"""


class Journal:
    """Durable ``first_seen`` store backed by SQLite.

    Lifecycle:
      1. ``open()`` — create/migrate the DB, set pragmas.
      2. ``load_first_seen()`` — bulk-read known first_seen at startup.
      3. ``record(track_id, hex, first_seen, last_seen)`` — buffer a write.
      4. ``run()`` as a background task — flush the buffer on the coalesce
         timer; ``stop()`` flushes the remainder and exits.
      5. ``close()`` — final flush + close the connection.

    Construction args:
      config: The validated ``JournalConfig`` (path + coalesce knobs).
    """

    def __init__(self, config: JournalConfig) -> None:
        self._config = config
        self._path = Path(config.path)
        self._conn: sqlite3.Connection | None = None
        # Warm-loaded first_seen, populated by load_first_seen() at startup.
        # The merger's restore lookup reads this in-memory map (a dict get),
        # never the DB — so a busy first poll does not hit disk per new track.
        self._first_seen: dict[str, datetime] = {}
        # Warm-loaded + record()-maintained last_seen. Read by last_seen_for for
        # history-aware alerts. Kept current on every record() (not just warm
        # load) so a within-session re-acquire reads the recent sighting.
        self._last_seen: dict[str, datetime] = {}
        # Pending writes, keyed by track_id so repeated updates within a flush
        # window collapse to one row (last_seen advances, first_seen min-wins).
        self._pending: dict[str, tuple[str | None, datetime, datetime]] = {}
        # Pending event rows (flag/alert transitions). Append-only — every
        # transition is a distinct historical row, so no per-key collapse.
        self._pending_events: list[tuple[str, str, str | None, datetime]] = []
        self._stop = asyncio.Event()
        self._flush_now = asyncio.Event()
        # Monotonic time of the last retention prune; gates the prune cadence.
        # None = never pruned -> the first check is always due. (Don't init to
        # 0.0: loop.time() is monotonic-from-boot, so on a freshly-booted host
        # 0.0 would wrongly look "recent" and delay the first prune ~an hour.)
        self._last_prune: float | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the DB, apply pragmas, and migrate to the current schema.
        Runs in a thread; raises if the path is unusable (fail-fast at start)."""
        await asyncio.to_thread(self._open_sync)
        log.info("journal_opened", path=str(self._path), version=SCHEMA_VERSION)

    def _open_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        self._conn = conn

    async def close(self) -> None:
        """Flush pending writes and close. Idempotent."""
        await asyncio.to_thread(self._flush_sync)
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def warm_load(self) -> int:
        """Bulk-read every known ``track_id -> (first_seen, last_seen)`` into
        memory at startup. Returns the count loaded. Call once after ``open()``."""
        self._first_seen, self._last_seen = await asyncio.to_thread(self._load_seen_sync)
        log.info("journal_warm_loaded", tracks=len(self._first_seen))
        return len(self._first_seen)

    def _load_seen_sync(self) -> tuple[dict[str, datetime], dict[str, datetime]]:
        assert self._conn is not None
        rows = self._conn.execute("SELECT track_id, first_seen, last_seen FROM track").fetchall()
        first = {track_id: _parse_dt(first_seen) for track_id, first_seen, _ in rows}
        last = {track_id: _parse_dt(last_seen) for track_id, _, last_seen in rows}
        return first, last

    def first_seen_for(self, track_id: str) -> datetime | None:
        """Restore lookup for the merger: the persisted ``first_seen`` for a
        track, or ``None`` if never seen. Reads the warm-loaded in-memory map
        (plus any first_seen recorded this session) — no disk IO."""
        recorded = self._pending.get(track_id)
        if recorded is not None:
            return recorded[1]
        return self._first_seen.get(track_id)

    def last_seen_for(self, track_id: str) -> datetime | None:
        """The persisted ``last_seen`` for a track, or ``None`` if never
        recorded. Warm-loaded and kept current by ``record()``, so it reflects
        both prior sessions and earlier sightings this session. Read in-memory —
        no disk IO. Used by history-aware alert criteria via the merger's
        prior-last-seen capture at track creation."""
        return self._last_seen.get(track_id)

    async def load_events(
        self, track_id: str | None = None
    ) -> list[tuple[str, str, str | None, datetime]]:
        """Read event rows (``(track_id, kind, detail, at)``), oldest first,
        optionally filtered to one track. The read substrate for Phase 5
        history-aware alert rules ("first time this month")."""
        return await asyncio.to_thread(self._load_events_sync, track_id)

    def _load_events_sync(
        self, track_id: str | None
    ) -> list[tuple[str, str, str | None, datetime]]:
        assert self._conn is not None
        if track_id is None:
            rows = self._conn.execute(
                "SELECT track_id, kind, detail, at FROM event ORDER BY at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT track_id, kind, detail, at FROM event WHERE track_id = ? ORDER BY at",
                (track_id,),
            ).fetchall()
        return [(tid, kind, detail, _parse_dt(at)) for tid, kind, detail, at in rows]

    # ------------------------------------------------------------------
    # Writes (buffered)
    # ------------------------------------------------------------------

    def record(
        self, track_id: str, hex_code: str | None, first_seen: datetime, last_seen: datetime
    ) -> None:
        """Buffer a track summary write. Coalesced: repeated calls for the same
        track within a flush window collapse — ``first_seen`` keeps the earliest
        and ``last_seen`` the latest. Wakes the flush loop once the buffer hits
        the event threshold. Never touches disk on the calling path."""
        existing = self._pending.get(track_id)
        if existing is not None:
            prev_hex, prev_first, prev_last = existing
            hex_code = hex_code if hex_code is not None else prev_hex
            first_seen = min(first_seen, prev_first)
            last_seen = max(last_seen, prev_last)
        self._pending[track_id] = (hex_code, first_seen, last_seen)
        # Keep the in-memory last_seen current (max), so last_seen_for reflects
        # this session's sightings, not just the warm-loaded boot snapshot.
        known = self._last_seen.get(track_id)
        self._last_seen[track_id] = last_seen if known is None else max(known, last_seen)
        self._maybe_trip_threshold()

    def record_event(self, track_id: str, kind: str, detail: str | None, at: datetime) -> None:
        """Buffer a history event row — a flag or alert transition. Append-only
        (each transition is its own row); pruned after the retention window.
        ``kind`` is one of ``flag_enter`` | ``flag_exit`` | ``alert_enter`` |
        ``alert_exit``; ``detail`` is the flag or rule name. Never touches disk
        on the calling path."""
        self._pending_events.append((track_id, kind, detail, at))
        self._maybe_trip_threshold()

    def _maybe_trip_threshold(self) -> None:
        """Wake the flush loop early once the combined buffer hits the event
        threshold, so a burst doesn't sit unbounded between timer ticks."""
        if len(self._pending) + len(self._pending_events) >= self._config.write_coalesce_events:
            self._flush_now.set()

    async def run(self) -> None:
        """Background flush loop: flush on the coalesce timer or when the buffer
        threshold trips, and prune expired event rows on a slow cadence. Runs
        as a task in the app TaskGroup until ``stop()``."""
        while not self._stop.is_set():
            # Wake on either the coalesce timer or an early threshold trip; a
            # timeout just means "timer fired, time to flush".
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._flush_now.wait(), timeout=self._config.write_coalesce_s
                )
            self._flush_now.clear()
            await asyncio.to_thread(self._flush_sync)
            await self._maybe_prune()

    async def stop(self) -> None:
        self._stop.set()
        self._flush_now.set()

    def _flush_sync(self) -> None:
        """Write both pending buffers (track summaries + events) in one
        transaction. Fail-soft: a DB error is logged and the buffers kept for
        the next attempt, never raised into the poll loop."""
        if self._conn is None or (not self._pending and not self._pending_events):
            return
        track_batch = self._pending
        event_batch = self._pending_events
        self._pending = {}
        self._pending_events = []
        track_rows = [
            (track_id, hex_code, _fmt_dt(first), _fmt_dt(last))
            for track_id, (hex_code, first, last) in track_batch.items()
        ]
        event_rows = [
            (track_id, kind, detail, _fmt_dt(at)) for track_id, kind, detail, at in event_batch
        ]
        try:
            # Upsert: first_seen is min-wins (keep the earliest ever recorded),
            # last_seen advances, hex backfills if it was NULL.
            if track_rows:
                self._conn.executemany(
                    """
                    INSERT INTO track (track_id, hex, first_seen, last_seen)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(track_id) DO UPDATE SET
                        hex = COALESCE(excluded.hex, track.hex),
                        first_seen = MIN(track.first_seen, excluded.first_seen),
                        last_seen = MAX(track.last_seen, excluded.last_seen)
                    """,
                    track_rows,
                )
            if event_rows:
                self._conn.executemany(
                    "INSERT INTO event (track_id, kind, detail, at) VALUES (?, ?, ?, ?)",
                    event_rows,
                )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning(
                "journal_flush_failed",
                error=str(exc),
                tracks=len(track_batch),
                events=len(event_batch),
            )
            # Keep both batches for the next flush rather than dropping history.
            for track_id, value in track_batch.items():
                self._pending.setdefault(track_id, value)
            self._pending_events = event_batch + self._pending_events

    # ------------------------------------------------------------------
    # Retention prune (event rows only — track summaries kept forever)
    # ------------------------------------------------------------------

    _PRUNE_INTERVAL_S: float = 3600.0
    """Run the prune at most hourly — it deletes by an indexed timestamp, so
    it is cheap, but there is no point doing it every flush."""

    async def _maybe_prune(self) -> None:
        now = asyncio.get_event_loop().time()
        if self._last_prune is not None and now - self._last_prune < self._PRUNE_INTERVAL_S:
            return
        self._last_prune = now
        await asyncio.to_thread(self._prune_sync)

    def _prune_sync(self) -> None:
        """Delete event rows older than the retention window. ``track`` rows
        (and thus ``first_seen``) are never pruned. Fail-soft."""
        if self._conn is None:
            return
        cutoff = datetime.now(UTC) - timedelta(days=self._config.retention_observations_days)
        try:
            cur = self._conn.execute("DELETE FROM event WHERE at < ?", (_fmt_dt(cutoff),))
            self._conn.commit()
            if cur.rowcount:
                log.info("journal_pruned_events", deleted=cur.rowcount)
        except sqlite3.Error as exc:
            log.warning("journal_prune_failed", error=str(exc))


def _fmt_dt(dt: datetime) -> str:
    """ISO-8601 UTC text — matches the payload serialization, lexically sortable."""
    return dt.astimezone(UTC).isoformat()


def _parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


__all__ = ["SCHEMA_VERSION", "Journal"]
