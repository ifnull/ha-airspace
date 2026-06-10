# Phase 2b implementation plan — durable history (SQLite journal)

**Goal (DESIGN "done when"):** restarting the service preserves `first_seen`
for every previously-observed track.

Phase 2b adds a SQLite journal so the project gains *memory* across restarts:
when an aircraft (or drone) reappears, its original `first_seen` is restored
rather than reset to "now." This unlocks history-aware data ("first seen 3
months ago") and is the substrate Phase 5's history-aware alert rules build on.

---

## 0. The problem, precisely

Today `AircraftState.from_first_observation` sets `first_seen = obs.observed_at`
**every time a track is newly created** — including the first time we see it
*after a restart*. So a daily-commuter aircraft you've tracked for months gets
a fresh `first_seen` of "now" on every service restart. `first_seen` is in the
published payload, so this is visible wrong data.

The journal makes `first_seen` durable: persist it once, restore it on the next
sighting after restart.

---

## 1. Scope decisions (locked by DESIGN, noted here)

- **SD-card friendly.** Write rows only on *meaningful events* (new track,
  flag-state change, alert ENTER/EXIT) — never every poll. Coalesce writes via
  a 30s timer OR a 50-event threshold, whichever first. WAL mode +
  `synchronous=NORMAL`. (DESIGN §2b.)
- **Two row classes, two retention policies:**
  - *Track summary* rows (`track_id`, `hex`, `first_seen`, `last_seen`) — kept
    **forever**. This is the durable `first_seen` store.
  - *Event* rows (flag transitions, alert ENTER/EXIT history) — pruned after
    `retention_observations_days` (default 90).
- **`every_poll: false`** by default — the full-observation firehose (~4M
  rows/day/receiver) is opt-in and out of scope for the core feature.
- **stdlib `sqlite3`** — no new dependency. Async-safe via a thread executor
  (`asyncio.to_thread`); the DB work is tiny and infrequent.
- **Hand-rolled migrations** via `PRAGMA user_version` — no migration framework.

---

## 2. Build order (two slices)

### Slice 1 — Journal + first_seen durability (the core "done when")
The store, schema, the restore-on-create path, and the coalesced writer. This
alone satisfies the DESIGN done-when. Fully testable with a temp DB file (no
network, deterministic clock).

- `Journal` class: open/migrate, `record_track(track_id, hex, first_seen,
  last_seen)`, `lookup_first_seen(track_id) -> datetime | None`, `prune()`,
  `close()`. Writes coalesced; reads direct.
- **Restore hook:** when the merger creates a new state, ask the journal for a
  prior `first_seen`; if present, use it instead of `observed_at`.
- **Startup warm-load:** load all known `(track_id -> first_seen)` into memory
  once at boot so the per-track restore is a dict lookup, not a DB hit per new
  track. (A receiver can surface 50+ new tracks in the first poll.)

### Slice 2 — Event history + retention
Persist flag-state changes and alert ENTER/EXIT as event rows; the periodic
prune of event rows older than the retention window. This is the substrate for
"first time this month" alerts (the *rules* themselves are Phase 5; 2b just
records the history).

- `record_event(track_id, kind, detail, at)` where kind ∈ {flag_enter,
  flag_exit, alert_enter, alert_exit}.
- Prune loop (in the journal's background task) deletes event rows past
  retention; summary rows are never pruned.

---

## 3. Schema (v1)

```sql
PRAGMA user_version = 1;

-- Durable per-track summary. first_seen is the payload; kept forever.
CREATE TABLE track (
    track_id   TEXT PRIMARY KEY,   -- ICAO hex or UAS id
    hex        TEXT,               -- NULL for non-ICAO (drones)
    first_seen TEXT NOT NULL,      -- ISO 8601 UTC
    last_seen  TEXT NOT NULL
);

-- Event history; pruned by retention_observations_days.
CREATE TABLE event (
    id        INTEGER PRIMARY KEY,
    track_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,       -- flag_enter | flag_exit | alert_enter | alert_exit
    detail    TEXT,                -- flag name or rule name
    at        TEXT NOT NULL        -- ISO 8601 UTC
);
CREATE INDEX event_at ON event(at);          -- for retention prune
CREATE INDEX event_track ON event(track_id); -- for history queries
```

Datetimes as ISO-8601 text (matches the payload serialization; human-readable
in the DB; lexically sortable for UTC). `track_id` is the source-agnostic key,
so drones and aircraft share one table cleanly.

---

## 4. Config

```yaml
journal:                          # Phase 2b — optional; omit = no persistence
  path: /data/journal.db
  retention_observations_days: 90
  write_coalesce_s: 30            # flush timer
  write_coalesce_events: 50       # flush threshold
  # every_poll: false             # full-observation firehose — out of scope
```

Absent section = no journal (current behavior, `first_seen` resets on restart —
the documented pre-2b state). Validated like every other section; `path` parent
dir must be creatable (fail-fast at startup if not).

---

## 5. Module layout

```
src/ha_airspace/
  journal.py        # NEW — Journal: sqlite store, migrations, coalesced writer
  config.py         # + JournalConfig
  merger.py         # restore first_seen on new-track creation (inject a lookup)
  app.py            # build + warm-load the journal; run its writer/prune task;
                    #   wire the first_seen lookup into the merger
  tracker.py        # emit flag/alert events to the journal (slice 2)
```

**Merger ↔ journal seam:** the merger gets an optional
`first_seen_for: Callable[[str], datetime | None]`. On new-track creation it
calls it; a hit overrides `observed_at`. Keeps the merger pure-ish (no DB
import — just a callable), consistent with how geometry/enrich are injected.

**Write path:** the journal owns a background task (like the DB loader). The
merger/tracker enqueue records; the task flushes on the coalesce timer/threshold
and runs the retention prune. Graceful shutdown flushes the pending buffer
(don't lose the last 30s of `first_seen` updates on SIGTERM).

---

## 6. Key design decisions

- **first_seen is min-wins.** If a track is somehow recorded with two different
  first_seen (clock skew, races), the *earliest* wins. `record_track` does an
  upsert that keeps `MIN(first_seen)` and updates `last_seen` to the latest.
- **Warm-load at boot, write-through after.** One bulk read at startup into a
  dict; thereafter the in-memory `AircraftState.first_seen` is authoritative
  and the journal is written behind it. The restore only matters at
  create-time.
- **The journal never blocks a poll.** All writes are buffered + flushed off
  the event loop; a slow/locked DB degrades to "history not saved," never to a
  stalled poll loop (same fail-soft posture as receivers and the DB loader).
- **Retention prune is summary-safe.** Only `event` rows are pruned; `track`
  rows (and thus `first_seen`) are never deleted. A track not seen for years
  still restores its original first_seen.
- **Drone first_seen too.** track_id-keyed, so drones get durable first_seen
  for free — "first time this drone was seen near home" is a real, novel signal.

---

## 7. Test strategy

- **Slice 1:**
  - `Journal` unit tests with a temp DB: record/lookup roundtrip, min-wins
    upsert, migration from empty (user_version 0 -> 1), reopen persistence.
  - **The done-when test:** create a track, persist, simulate restart (new
    Journal on the same file + new Merger with the lookup), re-observe the same
    track -> `first_seen` is the original, not the new observed_at.
  - Coalesced writer: N records buffered, flush on threshold and on timer
    (injected clock), flush-on-close loses nothing.
- **Slice 2:** event rows written on flag/alert transitions; prune deletes
  past-retention events but keeps track rows; history query returns events.
- **Integration:** a real on-disk DB across an app stop/start (reuse the
  app-level test harness) proving first_seen survives a full restart.
- **No network anywhere**; SQLite temp files only. Coverage stays 80%+.

---

## 8. Out of scope for 2b
- History-aware alert *rules* ("first time this month") — Phase 5 consumes the
  event history 2b records; the rule engine itself is later.
- `every_poll` full-observation logging — opt-in firehose, not the core feature.
- Photo enrichment — Phase 2c.

---

## 9. Open questions for sign-off
1. **Journal optional vs default-on?** Recommend **optional** (absent `journal:`
   = today's behavior), so ADS-B-only / ephemeral installs aren't forced to
   manage a DB file. Default path `/data/journal.db` is the add-on path; from
   source it needs an override (same caveat as `databases.cache_dir`).
2. **Event recording in slice 1 or 2?** Recommend deferring all event rows to
   slice 2 — slice 1 is purely first_seen durability (the done-when), smallest
   shippable unit.
3. **Async approach:** stdlib `sqlite3` + `asyncio.to_thread` (no new dep) vs
   adding `aiosqlite`. Recommend **stdlib** — the write volume is tiny and
   infrequent; a dependency isn't justified (CLAUDE.md: stdlib first).
