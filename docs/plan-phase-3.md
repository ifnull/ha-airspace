# Phase 3 implementation plan — multi-receiver merger + drone Remote ID

**Goal (DESIGN "done when"):** two receivers feeding the same hex produce one
canonical aircraft with `seen_by: [...]` populated and a deterministic canonical
pick — **and** a `dump3411` Remote ID feed surfaces drones (with operator
location) as a first-class source alongside ADS-B.

Phase 3 is the largest phase and the headline differentiator: it's where the
"what's flying near me, *including drones*" promise becomes real. Two threads
that share one new component (the merger): the multi-receiver ADS-B merge, and
the drone source. This doc proposes the build order, the model changes, the
merger design, and the slicing — to review before any code.

---

## 0. The honest starting point

The Phase-1 `AircraftTracker` is explicitly the "degenerate single-source
merger" (its own docstring + TODOs say so). Today:

- `app.py` runs **one poll loop per receiver, all feeding one tracker** under a
  lock. Canonical is always "latest observation wins" — there is no
  cross-receiver selection.
- **The model is NOT source-agnostic yet**, despite the Remote ID appendix
  claiming that was made "free in Phase 1." `AircraftObservation.hex: str` is
  ICAO-typed; `parse_hex` validates hex characters and lowercases. A drone's
  `id` (`"Spoofed_Serial_98067"`) is not hex and would be rejected. There is
  **no operator-location field** anywhere in the model.

So Phase 3 carries genuine model work, not just a new receiver. Good to surface
now rather than discover mid-slice.

---

## 1. Build order (four slices)

Ordered so the merger lands and is proven on ADS-B *before* the drone source
plugs into it — the drone work is lower-risk once the multi-source machinery is
real and tested.

### Slice 1 — Merger core (pure, no IO)
Extract a `Merger` that owns the canonical `AircraftState` dict and the
cross-receiver selection. Replaces the tracker's single-source `_ingest`. Pure:
takes per-receiver observation batches, produces merged states. The
lifecycle/geometry/nearest/enrich/alert/publish pipeline the tracker already
runs stays — the merger just changes how `canonical` and `by_receiver` are
computed. Fully testable with synthetic multi-receiver observations.

**Canonical selection (DESIGN §3, locked):** when ≥2 receivers report the same
hex in a cycle, pick canonical position by
`fresh seen_pos (<5s) → higher NIC → higher NAC_p → freshest seen_pos → higher
RSSI → alphabetical receiver name`. Deterministic.

### Slice 2 — Merger wiring (IO / app restructure)
Restructure `app.py`: poll loops feed the **merger** (not one tracker per the
current lock dance); the merger runs the downstream pipeline once per cycle over
all merged states. `seen_by` accumulates across receivers; band-merge (same hex
on 1090 + 978 → `bands={"1090","978"}`). Per-receiver health/stats topics
already exist; verify they still publish per-source.

### Slice 3 — Source-agnostic identity (model)
Generalize identity so a non-ICAO string key is first-class:
- `AircraftObservation` gains a source-agnostic identity (see §3 for the
  options to decide).
- `parse_hex` stays for ADS-B; Remote ID `id` bypasses it.
- This unblocks the drone source without it masquerading as an aircraft.

### Slice 4 — Remote ID source (drones)
`RemoteIdHttpReceiver` consuming `dump3411`'s `/data/remoteid.json` (the
contract in `FEED.md`), mapping detections → source-agnostic tracks with
`band="remoteid"`. Operator location as a first-class entity. Drone-specific HA
discovery (drone count, nearest drone, operator location), distinct from
aircraft entities. **No reference-DB lookup** for `remoteid` tracks.

**Done-when for the phase:** a real `dump3411` feed produces drone entities in
HA, *and* two ADS-B receivers on the same hex produce one deterministic merged
aircraft.

---

## 2. Module layout

```
src/ha_airspace/
  merger.py            # NEW — Merger: canonical selection, band/seen_by merge
  models.py            # source-agnostic identity (slice 3); operator location
  tracker.py           # slims to the post-merge pipeline, or folds into merger
  app.py               # poll loops -> merger -> pipeline (slice 2)
  receivers/
    remoteid.py        # NEW — RemoteIdHttpReceiver (slice 4)
    _remoteid_parse.py # NEW — remoteid.json -> observations (slice 4)
  mqtt/
    discovery.py       # + drone entities (slice 4)
    publisher.py       # + drone/operator topics (slice 4)
    payloads.py        # + DronePayload / OperatorPayload (slice 4)
```

**Relationship of merger ↔ tracker:** the tracker already does lifecycle →
geometry → enrich → alerts → publish over a state dict. The cleanest move is the
**merger replaces only the ingest/canonical step** and the tracker keeps the
rest — or we rename `AircraftTracker` to `Merger` and absorb it. Decide in
slice 1 (see §4 open questions). Either way the `# TODO(phase-3)` hooks already
mark the seams.

---

## 3. Source-agnostic identity — the one real design fork

A drone's identity is a string `id` (serial / CAA reg / session UUID), not an
ICAO hex. Three ways to model it; this needs sign-off because it touches the
published payload (the consumer contract):

- **(A) Add a generic `track_id` + keep `hex`.** `AircraftObservation` and
  `AircraftState` gain `track_id: str` (the dict key); `hex` stays as the
  ICAO-or-None field. Drones set `track_id=id, hex=None`. Aircraft set
  `track_id=hex`. Least disruptive to existing ADS-B payloads (still have
  `hex`); the merger keys on `track_id`. *Recommended.*
- **(B) Repurpose `hex` as the opaque key.** Store the drone `id` in `hex`.
  Simplest dict change, but lies about the field's meaning and risks a drone
  `id` hitting reference-DB lookups. Rejected by the appendix's own reasoning.
- **(C) Separate `DroneState` / `DroneTracker` parallel to aircraft.** Most
  honest separation, but doubles the merger/pipeline machinery and the "one
  airspace view" idea is lost. Heavy.

**The `band="remoteid"` discriminator is locked** (DESIGN appendix) regardless
of A/B/C — it's how the merger keeps drone and ICAO namespaces from
cross-matching.

**Operator location** is new data with no aircraft analogue. Proposal: an
optional `operator: OperatorInfo | None` on the drone observation (lat/lon/id/
alt_takeoff/seen), surfaced as its own HA entity — *not* shoehorned into the
aircraft position fields.

---

## 4. Key design decisions

- **Merger owns cadence (DESIGN §1).** Poll loops hand observations to the
  merger; the merger runs the pipeline once per tick. The current
  "one-tracker-under-a-lock" becomes the merger's job — the lock goes away.
- **Canonical selection is pure + deterministic** (the locked 6-step order),
  unit-tested with crafted multi-receiver disagreement.
- **Band merge:** same `track_id` on multiple bands → one state, `bands`
  accumulates, quality fields stay per-observation in `by_receiver`.
- **Expiry is cross-receiver:** a hex drops only when **no** receiver has seen
  it for `expiry_s` — not when one receiver loses it.
- **Drones never hit the reference DB** (no ICAO to look up); the enricher skips
  DB join for `band="remoteid"` tracks. Flag rules can still apply (e.g. a
  `drone` flag, or operator-proximity alerts).
- **Drone HA entities are separate:** `sensor.adsb_drone_count`,
  `sensor.adsb_nearest_drone`, operator location — distinct device/entities from
  aircraft so HA users can treat them independently.
- **Multi-transport / units already handled by the producer** (`FEED.md`): the
  feed arrives in imperial, single-track-per-id, so `RemoteIdHttpReceiver` is a
  thin mapper — most of the hard RID work lives in `dump3411`.

---

## 5. Test strategy

- **Slice 1:** pure merger unit tests — same hex two receivers, canonical pick
  for every tiebreaker rung, band merge, `seen_by` accumulation, cross-receiver
  expiry. Crafted observations, no IO.
- **Slice 2:** app-level integration with two `FileReceiver`s feeding the merger
  → assert one merged state, deterministic canonical, both receivers in
  `seen_by`. Reuse the FakePublisher pattern.
- **Slice 3:** model tests for the source-agnostic key; ADS-B path unchanged
  (regression), drone `id` accepted where hex would be rejected.
- **Slice 4:** `_remoteid_parse` against captured `remoteid.json` fixtures
  (we already have real captures — including the spoofed/edge-case drones);
  `RemoteIdHttpReceiver` via the same injected-transport pattern as
  `HttpJsonReceiver`; drone discovery/payload shape.
- **Live validation:** point at the real `dump3411` feed (dump3411.local:8754)
  and the two ADS-B receivers; prove a drone entity and a merged aircraft
  end to end, the way every prior slice was verified on live data.
- Coverage stays 80%+ on `src/`.

---

## 6. Out of scope for Phase 3
- Predictive ETA / closest-approach (schema reserved; impl Phase 5).
- True AGL via DEM for aircraft (drones get native AGL free; aircraft AGL is
  Phase 5).
- SQLite journal / `first_seen` durability (Phase 2b — independent of this).
- Orbit detection, web UI (Phase 5 / 4.5).

---

## 7. Open questions for sign-off
1. **Identity model: A, B, or C** from §3 (recommend **A** — `track_id` + keep
   `hex`). This is the load-bearing one; it shapes the payload contract.
2. **Merger vs tracker:** new `Merger` class with the tracker slimmed to the
   pipeline, or rename/absorb tracker into the merger? (Lean: extract `Merger`,
   keep the pipeline where it is, wire merger → pipeline.)
3. **Slice ordering:** merger-first (this plan) vs drone-source-first. Recommend
   merger-first — the drone source is lower-risk once the multi-source machinery
   is proven on ADS-B.
4. **Phase 2b before Phase 3?** The journal (2b) is smaller/independent. This
   plan assumes we do Phase 3 next; flag if 2b should interleave.
