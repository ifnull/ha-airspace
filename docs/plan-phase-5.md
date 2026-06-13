# Phase 5 implementation plan — polish

Phase 5 is the "delight/polish" basket. It splits into independent slices, built
one at a time:

1. **History-aware alerts** (this slice) — alert criteria backed by the 2b
   journal: fire only when a track is *new or returning* (not seen in N days).
2. **Orbit / loiter detection** (later) — cumulative-heading-change detector ->
   an `orbiting` flag; needs bounded position history on `AircraftState`.
3. **Predictive inbound** (later) — implement `predicted_eta_to_home_s` /
   `predicted_closest_approach_nm` + inbound alerts, guarded against
   airport-approach false positives.

---

## Slice 1 — History-aware alerts

**Goal:** a new alert match criterion `unseen_for_days: N` that matches only when
the track has **not been seen in the last N days** — i.e. it is brand new (never
recorded) or returning after an absence. Combines (AND) with the existing
criteria, so `flags: [military]` + `unseen_for_days: 30` = "a military aircraft I
haven't seen this month." This is the direct payoff of the journal built in 2b.

**The "prior last_seen" problem:** the journal's `last_seen` is updated live
while a track is active, so by alert time it reads "now". We need the value as of
*before* this session's sighting. Solution, symmetric with how `first_seen` is
already restored: capture the journal's prior `last_seen` onto the state at
track-creation time, before any live update.

### Changes

- **`models.py`** — `AircraftState.prior_last_seen: datetime | None = None`: the
  journal's `last_seen` for this track as of creation (None = never recorded).
  Distinct from the live `last_seen`.
- **`journal.py`** — warm-load `last_seen` alongside `first_seen` into an
  in-memory map; `record()` keeps it current (`max`); add
  `last_seen_for(track_id) -> datetime | None`. Read is in-memory (no IO), like
  `first_seen_for`. Captured at creation, so a within-session reappearance
  (record ran first) and a cross-restart reappearance both read correctly.
- **`merger.py`** — optional `last_seen_for` callable (mirrors `first_seen_for`);
  on new-track creation set `state.prior_last_seen`. IO-free (plain callable).
- **`config.py`** — `MatchBlock.unseen_for_days: float | None` (gt=0). A
  `Config` validator errors at load if any rule uses it while `journal` is unset
  (history-aware alerts need the ledger — fail fast, don't silently misfire).
- **`alerts.py`** — `rule_matches` gains an optional `now`; new branch: matches
  iff `prior_last_seen is None` OR `now - prior_last_seen >= N days`. The
  evaluator passes its injected clock.
- **`app.py`** — pass `last_seen_for=journal.last_seen_for` to the `Merger` when
  a journal is configured.
- **`config.example.yaml`** — document `unseen_for_days` on an alert rule.

### Behavior notes / edge cases

- **Never seen** (`prior_last_seen is None`, journal present) → novel → matches.
  The most novel case (a brand-new airframe) should fire.
- **No journal** → the criterion is rejected at config load, so this never
  silently fires-for-everything.
- **Within-session re-acquire** (track expired + reappeared) → `prior_last_seen`
  reflects the recent sighting → does NOT count as novel. Correct.

### Testing

- journal: `last_seen_for` warm-loads + reflects `record()`; survives restart.
- merger: `prior_last_seen` restored from the callback on new track; None when
  absent.
- `rule_matches`: never-seen matches; seen-recently doesn't; seen-long-ago does;
  combines with flags (AND).
- config: rule with `unseen_for_days` + no journal → `ConfigError`; with journal
  → loads.
- evaluator: an ENTER fires only once the novelty + other criteria all hold.
