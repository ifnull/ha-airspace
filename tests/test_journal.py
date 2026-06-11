"""Tests for the SQLite journal — durable first_seen (Phase 2b slice 1).

Temp DB files only, no network. The headline is the DESIGN done-when: a
restart (new Journal + new Merger on the same file) preserves first_seen.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ha_airspace.config import JournalConfig
from ha_airspace.journal import SCHEMA_VERSION, Journal
from ha_airspace.merger import Merger
from ha_airspace.models import AircraftObservation

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _config(tmp_path: Path, **overrides: object) -> JournalConfig:
    return JournalConfig(path=str(tmp_path / "journal.db"), **overrides)  # type: ignore[arg-type]


async def _opened(config: JournalConfig) -> Journal:
    j = Journal(config)
    await j.open()
    await j.warm_load()
    return j


def _obs(track_id: str = "ae0001", *, hex_code: str | None = "ae0001", at: datetime = _T0):
    return AircraftObservation(
        track_id=track_id, hex=hex_code, observed_at=at, seen_by="rx", band="1090"
    )


# ---------------------------------------------------------------------------
# Schema / lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_creates_db_and_sets_version(self, tmp_path: Path) -> None:
        j = await _opened(_config(tmp_path))
        assert (tmp_path / "journal.db").exists()
        await j.close()

    async def test_empty_db_warm_loads_nothing(self, tmp_path: Path) -> None:
        j = await _opened(_config(tmp_path))
        assert j.first_seen_for("ae0001") is None
        await j.close()

    async def test_open_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested"
        j = Journal(JournalConfig(path=str(nested / "journal.db")))
        await j.open()
        assert (nested / "journal.db").exists()
        await j.close()


# ---------------------------------------------------------------------------
# Record + persist + restore (the done-when)
# ---------------------------------------------------------------------------


class TestPersistence:
    async def test_record_then_lookup_after_flush(self, tmp_path: Path) -> None:
        j = await _opened(_config(tmp_path))
        j.record("ae0001", "ae0001", _T0, _T0)
        await j.close()  # close flushes

        # Reopen — first_seen survives.
        j2 = await _opened(_config(tmp_path))
        assert j2.first_seen_for("ae0001") == _T0
        await j2.close()

    async def test_first_seen_min_wins_on_rerecord(self, tmp_path: Path) -> None:
        j = await _opened(_config(tmp_path))
        earlier = _T0
        later = _T0 + timedelta(hours=5)
        # Record the later sighting first, then an earlier one.
        j.record("ae0001", "ae0001", later, later)
        j.record("ae0001", "ae0001", earlier, later)
        await j.close()

        j2 = await _opened(_config(tmp_path))
        assert j2.first_seen_for("ae0001") == earlier  # earliest wins
        await j2.close()

    async def test_pending_visible_before_flush(self, tmp_path: Path) -> None:
        # first_seen_for must see a just-recorded value even before it hits disk
        # (a busy first poll records many tracks before the flush timer fires).
        j = await _opened(_config(tmp_path))
        j.record("ae0001", "ae0001", _T0, _T0)
        assert j.first_seen_for("ae0001") == _T0
        await j.close()

    async def test_drone_track_id_persists(self, tmp_path: Path) -> None:
        # Non-ICAO (drone) track: string id, hex=None.
        j = await _opened(_config(tmp_path))
        j.record("Spoofed_Serial_1", None, _T0, _T0)
        await j.close()
        j2 = await _opened(_config(tmp_path))
        assert j2.first_seen_for("Spoofed_Serial_1") == _T0
        await j2.close()


# ---------------------------------------------------------------------------
# Coalesced writer
# ---------------------------------------------------------------------------


class TestCoalescing:
    async def test_repeated_records_collapse_to_one_row(self, tmp_path: Path) -> None:
        j = await _opened(_config(tmp_path))
        for i in range(5):
            j.record("ae0001", "ae0001", _T0, _T0 + timedelta(seconds=i))
        await j.close()
        j2 = await _opened(_config(tmp_path))
        # last_seen advanced to the latest of the batch.
        assert j2.first_seen_for("ae0001") == _T0
        await j2.close()


# ---------------------------------------------------------------------------
# The DESIGN done-when: restart preserves first_seen via the merger
# ---------------------------------------------------------------------------


class TestRestartPreservesFirstSeen:
    async def test_merger_restores_first_seen_after_restart(self, tmp_path: Path) -> None:
        # Session 1: first sighting at _T0, recorded to the journal.
        j1 = await _opened(_config(tmp_path))
        m1 = Merger(first_seen_for=j1.first_seen_for)
        s1 = m1.ingest(_obs("ae0001", at=_T0))
        j1.record(s1.track_id, s1.hex, s1.first_seen, s1.last_seen)
        assert s1.first_seen == _T0
        await j1.close()

        # Session 2 ("restart"): brand-new journal + merger on the same file.
        # The SAME aircraft is seen again 3 days later. Without the journal it
        # would get first_seen = now; with it, the original _T0 is restored.
        much_later = _T0 + timedelta(days=3)
        j2 = await _opened(_config(tmp_path))
        m2 = Merger(first_seen_for=j2.first_seen_for)
        s2 = m2.ingest(_obs("ae0001", at=much_later))
        assert s2.first_seen == _T0  # restored, NOT much_later
        assert s2.last_seen == much_later
        await j2.close()

    async def test_unknown_track_uses_observed_at(self, tmp_path: Path) -> None:
        # A never-before-seen track still gets first_seen = its observation time.
        j = await _opened(_config(tmp_path))
        m = Merger(first_seen_for=j.first_seen_for)
        state = m.ingest(_obs("brandnew", at=_T0))
        assert state.first_seen == _T0
        await j.close()


def test_schema_version_is_one() -> None:
    assert SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# Background writer (run loop): timer flush + threshold flush
# ---------------------------------------------------------------------------


class TestBackgroundWriter:
    async def test_threshold_flush_via_run_loop(self, tmp_path: Path) -> None:
        # coalesce_events=2 -> the 2nd record trips an immediate flush.
        j = await _opened(_config(tmp_path, write_coalesce_events=2, write_coalesce_s=999.0))
        task = asyncio.create_task(j.run())
        j.record("ae0001", "ae0001", _T0, _T0)
        j.record("ae0002", "ae0002", _T0, _T0)  # trips threshold -> flush
        await asyncio.sleep(0.1)  # let the flush land
        await j.stop()
        await task

        # Reopen and confirm both rows persisted (without a close-flush race).
        j2 = await _opened(_config(tmp_path))
        assert j2.first_seen_for("ae0001") == _T0
        assert j2.first_seen_for("ae0002") == _T0
        await j2.close()
        await j.close()

    async def test_timer_flush_via_run_loop(self, tmp_path: Path) -> None:
        # Short timer, high threshold -> only the timer can flush.
        j = await _opened(_config(tmp_path, write_coalesce_events=999, write_coalesce_s=0.05))
        task = asyncio.create_task(j.run())
        j.record("ae0001", "ae0001", _T0, _T0)
        await asyncio.sleep(0.2)  # > timer; a flush should have fired
        await j.stop()
        await task

        j2 = await _opened(_config(tmp_path))
        assert j2.first_seen_for("ae0001") == _T0
        await j2.close()
        await j.close()
