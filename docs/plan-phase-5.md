# Phase 5 implementation plan — polish

Phase 5 is the "delight/polish" basket. It splits into independent slices, built
one at a time:

1. **History-aware alerts** (this slice) — alert criteria backed by the 2b
   journal: fire only when a track is *new or returning* (not seen in N days).
2. **Orbit / loiter detection** (this slice) — a cumulative-heading-change
   detector that adds a derived `orbiting` flag.
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

---

## Slice 2 — Orbit / loiter detection

**Goal:** flag a track that is circling/loitering as `orbiting` — security-
relevant (police/surveillance helicopters, loitering drones, holding patterns).
It becomes a normal flag, so it composes with everything: alert rules
(`flags: ["orbiting"]`), `count_by_flag`, flag-transition journaling, payloads.

**Detection:** *signed* cumulative heading change over a sliding time window.
A straight track nets ~0; a sustained turn in one direction accumulates toward
360. Signed (not absolute) so zig-zag / S-turns cancel and don't false-positive;
a racetrack holding pattern still nets ~360 per circuit. Flag when
`abs(cumulative_turn) >= min_turn_deg` within the last `window_s`.

### Changes
- **`config.py`** — `OrbitConfig{enabled=False, window_s=120 (gt0),
  min_turn_deg=360 (gt0)}`; `orbit: OrbitConfig` on `Config`. Off by default.
- **`orbit.py`** (new) — `OrbitDetector`: per-track bounded heading history
  (`{track_id: [(t, heading)]}`), pruned to `window_s`. `update(state)` samples
  `canonical.track_deg` (skips when heading is None or `on_ground` — taxiing
  spins heading and would false-positive), recomputes the windowed turn, and
  adds `"orbiting"` to `state.flags` when over threshold. `forget(track_id)` on
  purge so history can't grow unbounded. In-memory only (no disk).
- **`tracker.py`** — optional `orbit` param; call `orbit.update(state)` right
  after `enricher.enrich` (which reassigns flags) and before flag-transition
  journaling, so orbit enter/exit is journaled and visible to alerts. On purge,
  `orbit.forget(track_id)`.
- **`app.py`** — build `OrbitDetector` when `orbit.enabled`, inject into tracker.
- **`config.example.yaml`** — document the `orbit:` block and that `orbiting`
  is a reserved computed flag usable in alert rules.

### Edge cases
- No heading / `on_ground` → not sampled → never flagged (avoids taxi spins).
- Sparse data can't accumulate 360 falsely; a single 90° course change nets 90.
- Heading wrap handled by shortest signed delta `((b-a+180) % 360) - 180`.

### Testing (`test_orbit.py` + config/tracker)
- angle delta wrap; full circle (0/90/180/270/0) → orbiting; straight → not;
  zig-zag cancels → not; window prunes old samples; on_ground/no-heading skip;
  `forget` drops history. Config defaults + validation. Tracker: flag added
  post-enrich; purge forgets.
