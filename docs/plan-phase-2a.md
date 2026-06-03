# Phase 2a implementation plan — rule engine

**Goal (DESIGN "done when"):** military aircraft appear on
`adsb/alert/military_close/<hex>` with full DB metadata in the payload.

Phase 2a is the enrichment rule engine: reference-DB join, declarative flag
rules, and stateful alert rules with ENTER/EXIT semantics. This doc proposes
the build order, the config schema (with deviations from DESIGN flagged), the
module layout, and the test strategy — to review before any code.

---

## 1. Build order (three slices)

Ordered so each slice ships independently green and the no-IO logic lands
before the network dependency, matching the Phase 1 rhythm.

### Slice 1 — Flag rules (pure, no network)
The enrichment pipeline skeleton + flag evaluator. Reads each `AircraftState`,
sets `state.flags`. Three flag types need **no database** and ship value
immediately:
- `emergency_squawk` — squawk in {7500, 7600, 7700}
- `callsign_prefix` — `state.canonical.flight` starts with a configured prefix
- `type_code` — `state.canonical.aircraft_type` in a configured set
- `category` *(new, see §2)* — `state.canonical.category` in a configured set

DB-backed flags (`military`, `interesting`, `privacy`) read `state.db_metadata`,
which is `{}` until slice 2. They simply never match yet — correct and
harmless. **Slice 1 is fully testable with synthetic `AircraftState`s.**

### Slice 2 — Reference DB loader (IO)
Mictronics + ADSBex download → parse in an executor → in-memory dict keyed by
hex → atomic swap → weekly refresh → populates `state.db_metadata` in the
enrichment pass. Lights up the DB-backed flags from slice 1. First Phase-2
runtime deps: HTTP downloads, on-disk cache, ~50–80 MB RAM.

### Slice 3 — Alert rules (stateful)
Alert evaluator with ENTER/EXIT transition detection, per-rule cooldown,
`adsb/alert/<rule>/<hex>` topics (ENTER publishes, EXIT clears retained), and
HA discovery `binary_sensor` per rule. Composes flags + distance + altitude.

---

## 2. Config schema — proposed, with deviations from DESIGN flagged

DESIGN splits rule config across **two** top-level sections:
`enrichment.flags` and `publish.alerts`. **I propose collapsing both under one
`enrichment` section.** Rationale:

- Flags and alerts are the same concern (rule evaluation); alerts *compose*
  flags. Splitting them across `enrichment` and `publish` means a user editing
  "the rules" touches two distant parts of the file.
- `publish` otherwise only holds `all_aircraft`, which is really an MQTT/topic
  concern that fits better near `mqtt`. (Out of scope for 2a — noting it.)
- One `enrichment` block keeps the mental model: "enrichment = DB join + flags
  + alerts," exactly the pipeline order.

**Proposed (deviation from DESIGN — needs sign-off):**

```yaml
databases:                      # NEW — slice 2
  cache_dir: "/data/db"
  refresh_interval_h: 168
  sources:
    - name: mictronics
      url: "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
      enabled: true
    - name: adsbexchange
      url: "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz"
      enabled: true

enrichment:                     # NEW — slices 1 + 3
  flags:                        # slice 1
    military:
      sources: ["mictronics:mil", "adsbexchange:mil"]
    interesting:
      sources: ["adsbexchange:interesting"]
    privacy:
      sources: ["adsbexchange:pia", "adsbexchange:ladd"]
    emergency_squawk:
      squawks: ["7500", "7600", "7700"]
    callsign_prefix:
      patterns: ["RCH", "SAM", "MEDEVAC"]
    type_code:
      types: ["B52", "C17", "U2"]
    # category: { categories: ["A7"] }   # optional new flag type

  alerts:                       # slice 3
    cooldown_s: 60
    rules:
      - name: military_close
        match:
          flags: ["military"]            # OR within a key
          max_distance_nm: 30            # AND across keys
          watchpoint: home
      - name: emergency_anywhere
        match:
          flags: ["emergency_squawk"]
```

**`match` semantics (locked by DESIGN, kept verbatim):**
- Different top-level keys in a `match` → **AND**.
- A list within one key → **OR**.
- `flags: [a, b], category: [A7]` ⇒ `(a OR b) AND A7`.

**Each flag config is a tagged union by its key field:** `sources` →
DB-backed; `squawks` → squawk match; `patterns` → callsign prefix; `types` →
type code; `categories` → emitter category. Pydantic validates that exactly one
discriminator is present per flag. Unknown flag-config shapes fail at load
(strict mode), consistent with Phase 1.

**Backward-compat:** Phase 1 configs have no `databases`/`enrichment` — both
sections default to empty/absent, so existing configs keep working and the
service runs with zero flags (today's behavior).

### Open question for sign-off
DESIGN calls the `match` block "load-bearing — get it right in v1." Adopting
the `enrichment.{flags,alerts}` layout means **updating DESIGN.md** to match
(its config example + the §4 snippets). Two options:
- **(A)** Take the deviation, update DESIGN.md in the same PR. *(recommended)*
- **(B)** Follow DESIGN literally (`enrichment.flags` + `publish.alerts`).

---

## 3. Module layout

```
src/ha_airspace/
  config.py            # + DatabasesConfig, EnrichmentConfig, FlagConfig(union),
                       #   AlertsConfig, AlertRule, MatchBlock  (slices 1–3)
  enrichment.py        # NEW — Enricher: orchestrates db-join -> flags -> alerts
  flags.py             # NEW — pure flag-rule evaluation (slice 1)
  alerts.py            # NEW — stateful alert evaluator, ENTER/EXIT (slice 3)
  databases/
    __init__.py
    loader.py          # NEW — async refresh, atomic swap (slice 2)
    mictronics.py      # NEW — CSV parser (slice 2)
    adsbexchange.py    # NEW — JSON parser (slice 2)
  models.py            # AircraftState.flags / db_metadata already exist
  mqtt/
    discovery.py       # + per-alert binary_sensor entities (slice 3)
    publisher.py       # + publish_alert / clear_alert (slice 3)
    payloads.py        # + AlertPayload (slice 3)
```

**Wiring:** the tracker already calls geometry per poll. The `Enricher` runs
right after geometry, before publish: `db-join → flags → alerts`. The tracker
gains an injected `Enricher` (optional; absent = today's behavior). Alert
events the enricher emits are handed to the publisher.

`# TODO(phase-3)`: enrichment runs per-state in the single-source tracker;
under the merger it runs once per canonical state per cycle — same call site.

---

## 4. Key design decisions

- **Flag eval is pure and order-independent.** `evaluate_flags(state, config)
  -> set[str]`. No mutation of inputs; the enricher assigns the result to
  `state.flags`. Re-runs every poll (flags can change as squawk/position
  changes); cheap (set ops over a handful of rules).
- **DB metadata merge priority (DESIGN §4):** ADSBex `mil` wins over Mictronics
  on conflict. The loader produces one merged dict per hex; the enricher just
  reads `state.db_metadata`.
- **`sources: ["mictronics:mil"]`** is parsed as `(db, field)` pairs; a flag
  matches if **any** referenced source-field is truthy in `db_metadata`.
- **Alert state is keyed by `(rule_name, hex)`** with last-state + last-exit
  timestamp for cooldown. Lives in the alert evaluator, injected clock for
  tests (CLAUDE.md). ENTER → publish retained; EXIT → clear retained; re-ENTER
  blocked for `cooldown_s` after EXIT.
- **`max_alt_agl_ft`** = MSL − watchpoint `elevation_m` (v1 approximation,
  documented; true AGL is Phase 5). A rule using it on a watchpoint without
  `elevation_m` fails validation at load.
- **DB loader never wipes a good copy on failed refresh** (DESIGN §2):
  download to temp, parse, validate non-empty, then atomic-swap; on any failure
  keep the existing dict and log.
- **Snapshot-on-enrich:** enricher captures `db = self._db.current` once per
  pass so a mid-cycle refresh swap can't tear reads (DESIGN §2).

---

## 5. Test strategy

- **Slice 1:** pure unit tests over synthetic `AircraftState`s — every flag
  type, the AND/OR `match` precedent, empty-config no-op, DB-backed flags
  no-match when `db_metadata` is `{}`.
- **Slice 2:** parse real captured DB samples (small fixtures, gzip’d) — no
  network in unit tests; the download path is an integration test behind the
  `integration` marker. Atomic-swap + failed-refresh-keeps-old as unit tests
  with a fake fetcher.
- **Slice 3:** ENTER/EXIT transitions and cooldown with an injected clock;
  alert publish/clear via the existing FakePublisher pattern; per-alert
  discovery entity shape.
- **Live validation:** against the real 1090 feed — emergency squawk won't
  appear on demand, but callsign-prefix and category flags will match real
  traffic (e.g. an `A3` category or an airline prefix), provable end to end.
- Coverage target stays 80%+ on `src/`.

---

## 6. Out of scope for 2a (later phases)
- SQLite journal / `first_seen` durability → **2b**
- Photo enrichment, predictive fields → **2c / Phase 5**
- True AGL via DEM → **Phase 5**
- Moving `publish.all_aircraft` near `mqtt` → noted, not now
