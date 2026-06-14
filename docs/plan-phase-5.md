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

---

## Slice 3 — Predictive inbound

**Goal:** populate the reserved `predicted_closest_approach_nm` /
`predicted_eta_to_home_s` fields and add composable "inbound" alert criteria —
without the airport-exclusion machinery DESIGN floated. The geometry is pure and
risk-free; false-positive control is the operator's, via AND-ing the existing
altitude/distance criteria (explicit over clever).

**Geometry (closest point of approach):** in `geo.closest_point_of_approach`,
project the aircraft into local equirectangular nm relative to a watchpoint, take
the velocity vector from `track_deg` + `ground_speed_kt`, and solve
`t_cpa = -(r·v)/(v·v)`:
- approaching (`t_cpa > 0`) -> CPA distance at `t_cpa`; eta = `t_cpa` seconds
- departing / stationary (`t_cpa <= 0` or speed 0) -> CPA = current distance, eta = `None`

**Relative to the primary watchpoint** (the fields are `*_to_home`; "home" is the
primary). Computed in the tracker alongside geometry; set to `None` unless the
canonical obs has position + `track_deg` + `ground_speed_kt >= MIN_SPEED_KT` and
is airborne (so parked/taxiing/hovering don't emit garbage).

**Alert criteria (MatchBlock):**
- `max_closest_approach_nm` — matches when the track is *approaching* (eta not
  None) and its predicted CPA <= this. (A departing track never matches, even if
  currently close.)
- `within_eta_s` — optional; additionally require eta <= this. Only valid with
  `max_closest_approach_nm` (validated).

Compose with the existing keys, e.g. `max_closest_approach_nm: 5` +
`max_alt_agl_ft: 10000` + `max_distance_nm: 40` = "heading to within 5 nm of
home, below 10k ft, currently within 40 nm."

### Changes
- `geo.py` — `closest_point_of_approach(wp_lat, wp_lon, ac_lat, ac_lon,
  track_deg, ground_speed_kt) -> (cpa_nm, eta_s | None)`.
- `tracker.py` — compute the two predicted fields for the primary watchpoint in
  the geometry pass; clear to `None` when prediction isn't possible.
- `config.py` — `MatchBlock.max_closest_approach_nm` / `within_eta_s` (gt 0);
  add to the "at least one condition" set; validator: `within_eta_s` needs
  `max_closest_approach_nm`.
- `alerts.py` — `rule_matches` inbound branch (approaching + CPA <= bound +
  optional eta bound).
- payloads already carry the fields (read-through) — now populated.
- `config.example.yaml` — document an inbound rule.

### Testing
- geo: head-on (cpa~0, eta>0); tangential (cpa = perpendicular distance);
  departing (eta None, cpa = current); stationary.
- tracker: fields set for an approaching track; None when no track/speed/on_ground.
- config: within_eta_s requires max_closest_approach_nm; both gt 0.
- alerts: approaching within bound matches; departing doesn't; beyond eta
  doesn't; ANDs with flags/altitude.
