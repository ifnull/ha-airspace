# ha-airspace — Design Document

A multi-source ADS-B enrichment service that consumes `aircraft.json` from one or more receivers, joins against reference databases, applies tagging/alert rules, and publishes to MQTT for Home Assistant (or any MQTT consumer).

---

## Problem statement

Existing PiAware/dump1090 setups expose a flat `aircraft.json` feed. Surfacing *interesting* aircraft (military, emergency, low helicopters, privacy-flagged tail numbers) currently requires hand-rolled REST sensors and Jinja templates in Home Assistant. That approach:

- Doesn't scale past a handful of sensors
- Can't easily join against external databases (Mictronics, ADSBexchange basic-ac-db)
- Can't do stateful logic (orbit detection, alert deduplication, "first seen" tracking)
- Doesn't support multiple receivers cleanly (1090 + 978, multiple sites)
- Forces every user to rebuild the same wheel

This service centralizes that work and exposes a clean MQTT interface.

---

## Goals

- **Universal receiver support.** Works with stock FlightAware appliances, custom PiAware builds, readsb, dump1090-mutability, dump978-fa, tar1090, and recorded JSON files.
- **Multi-receiver from day one.** 1090 + 978, multiple physical sites, federated setups.
- **Zero changes to receivers.** The service only consumes the HTTP `aircraft.json` endpoint. No filesystem access on the receiver, no plugins, no configuration changes on the dump1090 side.
- **Clean HA integration.** MQTT discovery creates entities automatically. Users get a working dashboard without writing templates.
- **Distributable three ways:** HA add-on (primary), Docker image, pip package. Same codebase.

## Non-goals

- Not a replacement for dump1090 / readsb. We consume their output.
- Not a feeder. Users continue feeding FlightAware/ADSBex/etc. independently.
- Not a flight tracking website. No public-facing UI; MQTT is the interface.
- Not a HACS custom integration (see "Distribution" section for rationale).

---

## Positioning & differentiation

The "ADS-B in Home Assistant" space already has projects. This one is
deliberately *not* competing on the same axis as most of them. The landscape,
as of mid-2026, splits cleanly:

**Cloud-API projects** (the popular ones) poll a hosted API, no local hardware:
- `home-assistant-flightradar24` (~459★) — the market leader; FlightRadar24 API.
- `whats-that-plane` (~91★) — FR24 API, "cone of vision" filtering.
- `flightradar-flight-card` (~36★) — a Lovelace card on top of the FR24 integration.
- `SkyRadar Fusion` (~7★) — Airplanes.Live + FR24 hybrid.

**Local-receiver projects** (our camp) consume a receiver on your LAN:
- `adsb-aircraft-tracker` (~10★, MIT) — the closest comparable: local dump1090/
  tar1090, tar1090-db military list, emergency squawks, nearest-aircraft
  sensors, mobile notifications. A polling custom-integration.

We are not first, and "track nearby aircraft + flag military from a local
receiver" is not by itself differentiated — `adsb-aircraft-tracker` ships that
today. The differentiation is four specific, defensible wedges:

1. **Drones / Remote ID — nobody else in the HA ecosystem has this.** None of
   the projects above touch unmanned traffic. We ingest ASTM F3411 Remote ID
   (via the companion `dump3411` detector) as a first-class source alongside
   ADS-B. "What's flying near me, *including drones and their operators*" is a
   category, not a feature, and it is timely as Remote ID mandates expand. This
   is the headline.
2. **Architecture: a standalone MQTT service with a multi-source merger** —
   not a single-source polling integration. We merge 1090 + 978 + Remote ID,
   multiple receivers, multiple sites, dedup by position quality (NIC → NAC_p →
   …). No competitor merges sources. This is the technical moat for the
   serious multi-receiver / homelab audience.
3. **Reference-DB depth.** Two databases (Mictronics + ADSBexchange) merged
   with conflict-resolution priority, full dbFlags decode (military,
   interesting, PIA, LADD) — not just a single military list.
4. **Built to last.** Strict typing, a large test suite, broker integration
   tests, phased delivery. Most competitors are explicitly hobby projects;
   for something meant to become household infrastructure, durability is itself
   a differentiator.

**The audience is people who already run a receiver** (and increasingly, a
drone detector) — not the casual FR24 user. We will not out-polish the
zero-hardware cloud apps on first-run experience, and we should not try. We
win on local-first, multi-source, drones, and rigor.

---

## Architecture overview

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Receiver A     │  │  Receiver B     │  │  Receiver C     │
│  (dump1090)     │  │  (dump978)      │  │  (readsb)       │
│  aircraft.json  │  │  aircraft.json  │  │  aircraft.json  │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │ HTTP              │ HTTP              │ HTTP
         └───────────────────┼───────────────────┘
                             ▼
                  ┌─────────────────────┐
                  │ Enrichment Service  │  ← runs anywhere
                  │ (multi-source merge,│
                  │  DB join, tagging,  │
                  │  alert evaluation)  │
                  └──────────┬──────────┘
                             │ MQTT
                             ▼
                  ┌─────────────────────┐
                  │  Home Assistant     │
                  │  (or any consumer)  │
                  └─────────────────────┘
```

### Why this split

**Universal interface: `aircraft.json` over HTTP.** Every dump1090 variant exposes this. It works for locked-down appliances where users can't install anything, custom builds, remote receivers over Tailscale/VPN, and dockerized setups. Filesystem access on the receiver is not portable; HTTP is.

**Enrichment is its own service, not a HA component.** It needs reference databases in memory, retry/backoff logic for HTTP, stateful per-aircraft tracking. That's a real Python application, not a Jinja template. Keeping it outside HA means it runs the same way whether the consumer is HA, Grafana, or a CLI tool.

**MQTT as the boundary.** Aircraft state is event-driven and ephemeral — appears, updates rapidly, disappears. MQTT models that natively. HA's MQTT discovery handles entity creation without us writing a custom integration.

---

## Component design

### 1. Receiver source abstraction

The core abstraction: every receiver is "a thing that periodically yields a list of aircraft observations, plus optional receiver metadata."

```python
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Optional

@dataclass(frozen=True)
class ReceiverLocation:
    lat: float
    lon: float
    alt_m: Optional[float] = None
    source: str = "unknown"  # "receiver_json" | "config" | "default"

@dataclass(frozen=True)
class AircraftObservation:
    """A single observation of an aircraft from a single receiver at a moment in time.

    Intentionally close to dump1090's aircraft.json schema so mapping is cheap,
    but normalized: units are explicit, missing fields are None (not absent),
    and we add provenance (which receiver, when).
    """
    hex: str                          # ICAO 24-bit, lowercase, no leading 'tilde'
    observed_at: datetime             # when WE saw it (not when receiver saw it)
    seen_by: str                      # receiver name from config

    # Identity
    flight: Optional[str] = None      # callsign, stripped/uppercased
    registration: Optional[str] = None
    squawk: Optional[str] = None

    # Position
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_baro_ft: Optional[int] = None
    alt_geom_ft: Optional[int] = None
    nav_altitude_mcp_ft: Optional[int] = None  # selected altitude

    # Movement
    ground_speed_kt: Optional[float] = None
    track_deg: Optional[float] = None
    vertical_rate_fpm: Optional[int] = None
    on_ground: Optional[bool] = None

    # Signal quality (per-receiver, useful for source picking later)
    rssi_dbfs: Optional[float] = None
    seen_pos_age_s: Optional[float] = None   # how stale is this position
    seen_age_s: Optional[float] = None       # how stale is any data

    # Type/category from the receiver itself (not DB join)
    category: Optional[str] = None    # ADS-B emitter category, e.g. "A3", "B6"
    aircraft_type: Optional[str] = None  # readsb-only: ICAO type designator
    band: str                         # "1090" | "978" — REQUIRED, no default
                                      #   (silent miscategorization is a footgun)

    # Position-quality fields from dump1090/readsb (used by Phase 3 multi-receiver
    # canonical-position selection; Phase 1 just stores them).
    nic: Optional[int] = None         # Navigation Integrity Category (0–11)
    nac_p: Optional[int] = None       # Navigation Accuracy Category, position


class ReceiverSource:
    """Abstract base. Concrete implementations:
      - HttpJsonReceiver  (dump1090-fa, dump1090-mutability, readsb, dump978-fa)
      - FileReceiver      (replay a recorded aircraft.json for testing)
      - TestReceiver      (synthetic data for unit tests)

    Pull-based interface: the merger owns the polling cadence and calls fetch()
    per receiver on its own timer. Receivers do not self-pace; they handle a
    single request per fetch() call and return.
    """
    name: str
    band: str

    async def location(self) -> Optional[ReceiverLocation]:
        """One-shot fetch of receiver location. Cached by caller."""
        ...

    async def fetch(self) -> list[AircraftObservation]:
        """Single poll cycle.

        Fail-fast semantics: one HTTP request, no internal retry. On transient
        failure (timeout, connect error, malformed JSON, schema drift) returns
        an empty list AND increments an internal consecutive-failure counter
        exposed via health(). After 3 consecutive failures, health() reports
        online=False until the next successful fetch resets the counter.

        This keeps single-poll latency under the 1s budget every cycle and
        makes flakiness immediately visible via metrics, instead of being
        smoothed away by hidden retries.
        """
        ...

    async def health(self) -> dict:
        """Status for diagnostics. Required keys:
          online: bool                 # False after 3 consecutive fetch failures
          last_success: datetime
          consecutive_failures: int    # resets on first success
          aircraft_count: int          # last successful fetch's count
          messages_per_sec: float
        Implementations may include extras.
        """
        ...
```

**Slow-poll skip policy.** If a receiver's `fetch()` is still in flight when the
merger's next tick fires, the merger skips that tick for that receiver and
increments a `slow_polls_total{receiver=...}` Prometheus counter. No queueing,
no parallel polls. A warning is logged at 5+ skipped within one minute.

**Key design decisions:**

- **`AircraftObservation` is per-receiver, not deduplicated.** The merger downstream produces canonical aircraft from N observations. Keeping observations separate preserves "which receivers saw this" data and lets us pick the best position when receivers disagree.
- **Units are baked into field names.** `alt_baro_ft`, `ground_speed_kt`, `vertical_rate_fpm`. dump1090 already uses these units; making them explicit prevents future confusion.
- **`observed_at` is when *we* polled, not when the receiver saw it.** dump1090's `now` field is the receiver's clock and may be skewed. We trust our own clock for staleness math; the receiver's `seen` deltas tell us how stale relative to it.
- **`band` is first-class and required.** 1090 vs 978 affects merging logic and is genuinely user-visible. No default — config errors fail at validation.
- **`fetch()` is pull-based and fail-fast.** Retries do not live inside `fetch()`. If the merger needs different cadence, it changes its own timer.
- **`health()` returns dict, not a typed object.** Standardize required keys, allow implementation-specific extras.

#### Receiver path auto-detection

dump1090 variants expose `aircraft.json` at different paths. On config validation, probe these in order and let users override:

| Variant              | Common path                                    |
|----------------------|------------------------------------------------|
| dump1090-fa (PiAware)| `/skyaware/data/aircraft.json`                 |
| dump978-fa           | `/skyaware978/data/aircraft.json`              |
| dump1090-mutability  | `/dump1090/data/aircraft.json`                 |
| readsb / tar1090     | `/tar1090/data/aircraft.json`, `/data/aircraft.json` |

Sibling endpoint `receiver.json` provides receiver lat/lon when configured; pull it once at startup, fall back to user-supplied coords.

### 2. Reference database loader

Two databases, refreshed weekly via background task:

| Source           | URL                                                                       | Provides                                |
|------------------|---------------------------------------------------------------------------|-----------------------------------------|
| Mictronics       | `https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz`         | hex → reg, type, operator, mil flag     |
| ADSBexchange     | `https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz`        | hex → mil, interesting, pia, ladd flags |

Loaded into in-memory dict keyed by hex (lowercase). Service runs without them — enrichment just becomes less rich. On refresh, atomically swap the dict reference; never partial-update.

**Design constraints:**

- Combined memory footprint: roughly 50–80 MB. Acceptable for HA add-on context.
- Refresh must not block the main event loop. Run in a thread executor (`asyncio.to_thread`).
- Cache to disk via atomic write-then-rename — one big sequential write per refresh, gentle on SD cards.
- Never let a failed refresh wipe an existing in-memory copy.

**Snapshot-on-enrich:** the in-memory dict is replaced atomically on refresh
(`self._db_dict = new_dict`). To avoid torn reads when an enrichment cycle does
multiple lookups across a refresh boundary, every enrichment function captures
a local reference at entry:

```python
async def enrich(self, state: AircraftState) -> None:
    db = self._db_dict          # snapshot; immune to subsequent rebinding
    meta = db.get(state.hex, {})
    # ... further reads from `db`, never from self._db_dict
```

Python dict-name rebinding is atomic; a function holding the prior reference is
unaffected by the swap. No lock, no `ContextVar` needed.

### 3. Multi-source merger

The interesting algorithm. Given N receivers polling independently, produce a single canonical view of the airspace.

**Inputs:** stream of `AircraftObservation` lists from each `ReceiverSource`.
**Output:** an `AircraftState` dict keyed by hex, updated continuously.

```python
@dataclass
class AircraftState:
    hex: str
    first_seen: datetime
    last_seen: datetime
    seen_by: set[str]              # all receivers that have ever seen this hex
    bands: set[str]                 # {"1090"} or {"978"} or both

    # Canonical position — picked from the best observation
    canonical: AircraftObservation
    canonical_source: str           # which receiver provided canonical

    # Per-receiver latest observations (for diagnostics)
    by_receiver: dict[str, AircraftObservation]

    # Enrichment results (populated by enricher, not merger)
    flags: set[str] = field(default_factory=set)
    db_metadata: dict = field(default_factory=dict)
    distance_to: dict[str, float] = field(default_factory=dict)  # watchpoint name → nm
    bearing_to: dict[str, float] = field(default_factory=dict)   # watchpoint name → deg

    # Predictive (Phase 2c reserves the schema; Phase 5 implements with airport
    # exclusion zones, altitude floor/ceiling, and orbit detection to avoid
    # false-positive alerts on approach traffic at nearby airports).
    predicted_eta_to_home_s: Optional[float] = None       # always None pre-Phase-5
    predicted_closest_approach_nm: Optional[float] = None # always None pre-Phase-5
```

**Canonical position selection (Phase 3).** When the same hex appears on multiple receivers in the same poll cycle, pick canonical position by:

1. Prefer observations with `seen_pos_age_s` < 5 (fresh position)
2. Among those, prefer higher `nic` (Navigation Integrity Category — aircraft-broadcast position quality)
3. Among those, prefer higher `nac_p` (Navigation Accuracy Category, position)
4. Among those, prefer freshest `seen_pos_age_s`
5. Among those, prefer highest `rssi_dbfs` (signal strength as a fallback proxy)
6. Tiebreak: alphabetical receiver name (deterministic)

Rationale: `nic`/`nac_p` are the aircraft's own self-reported position quality.
`rssi_dbfs` is a proxy for it at best — a nearby receiver getting a multipath
signal can have stronger RSSI but worse positional accuracy than a clean
line-of-sight receiver further out.

**Aircraft expiry.** Drop from state when no receiver has seen the hex for `expiry_s` (default 60s). On expiry, publish a "removed" event so consumers can clean up.

**1090 + 978 merging.** Same hex on both bands is the same aircraft; merge into one state with `bands = {"1090", "978"}`. Position can come from either; band-specific quality fields (rssi, message rate) stay per-observation.

### 4. Enrichment pipeline

After the merger updates `AircraftState`, run enrichment:

1. **DB join (Phase 2a).** Look up hex in Mictronics + ADSBex, populate `db_metadata` with merged fields (registration, type code, operator, military flag, etc.). Conflicts resolved by source priority (ADSBex's `mil` flag wins over Mictronics if they disagree, since ADSBex updates more frequently for military).
2. **Geometry.** Compute distance/bearing from each configured watchpoint (see Configuration). Haversine is fine; vincenty is overkill. `state.distance_to` and `state.bearing_to` are dicts keyed by watchpoint name.
3. **Flag evaluation (Phase 2a).** Apply each flag rule from config; populate `state.flags`.
4. **Alert evaluation (Phase 2a).** Apply each alert rule; produce alert events for state transitions (rule entered/exited). After EXIT, a per-rule cooldown (default 60s) blocks re-ENTER for the same hex to prevent thrashing alerts when a flag oscillates.

**Phase 2 implementation slices:**
- **2a:** DB loader + flag rules + alert rules + alert ENTER/EXIT semantics.
- **2b:** SQLite journal (`first_seen` durability, history-aware alerts).
- **2c:** Photo enrichment + predictive schema fields (impl deferred to Phase 5).

**Flag rules are declarative.** Adding a new flag is a config change, not a code change. Flags and alerts live under one `enrichment` section (consolidated in Phase 2a — see the Configuration section). Each flag carries exactly one matcher, identified by its field name:

```yaml
enrichment:
  flags:
    military:
      sources: ["mictronics:mil", "adsbexchange:mil"]   # OR logic across sources (DB-backed)
    callsign_prefix:
      patterns: ["RCH", "SAM", "EVAC", "MEDEVAC"]        # callsign starts-with
    emergency_squawk:
      squawks: ["7500", "7600", "7700"]                  # exact squawk
    heavy_mil:
      types: ["B52", "C17", "U2"]                        # ICAO type designator
    rotorcraft:
      categories: ["A7"]                                  # ADS-B emitter category
```

Matcher kinds: `sources` (DB-backed; truthy `db_metadata` field), `patterns` (callsign prefix), `squawks` (exact), `types` (ICAO type designator), `categories` (emitter category). The `categories` matcher was added in Phase 2a so rules like `low_helicopter` (below) can reference a flag without a reference database.

**Alert rules compose flags** (also under `enrichment`):

```yaml
enrichment:
  alerts:
    rules:
      - name: "military_close"
        match:
          flags: ["military"]            # list within key = OR
          max_distance_nm: 30            # different keys = AND
          watchpoint: "home"
```

**`match` block semantics (locked):**
- **Different top-level keys** in the same `match` are combined with **AND** (all must be true).
- **Lists within a single key** are combined with **OR** (any item satisfies).
- Example: `flags: [a, b], category: [A7]` means `(a OR b) AND A7`.

This is a load-bearing spec — get it right in v1 so future config changes don't ambiguously redefine it.

### 5. MQTT publisher

Topic structure:

```
adsb/
├── status                                 # service-level: online/offline (LWT)
│
├── receiver/
│   └── <receiver_name>/
│       ├── status                         # online/offline (LWT)
│       ├── stats                          # JSON: msgs/sec, aircraft count, last fetch
│       └── location                       # JSON: receiver lat/lon
│
├── aircraft/
│   └── <hex>                              # JSON: full enriched aircraft state
│                                          # retained, expires when aircraft drops
│
├── alert/
│   └── <rule_name>/
│       └── <hex>                          # JSON: triggering aircraft + rule context
│                                          # retained briefly, cleared on rule exit
│
└── summary/
    ├── nearest                            # JSON: closest aircraft (any)
    ├── nearest_interesting                # JSON: closest matching any flag
    ├── count                              # int: total aircraft in coverage
    └── count_by_flag                      # JSON: {military: 0, interesting: 2, ...}
```

**Publication strategy:**

- **`adsb/aircraft/<hex>`** publishes on every state change. **Power-user wildcard topic only.** This is NOT auto-discovered as HA entities — turning every aircraft into a discovered entity would melt HA's entity registry within a single busy day. Users who want per-aircraft data subscribe with their own consumer (Grafana, Node-RED, custom scripts). Retained, so late subscribers see current state; `null` payload clears on aircraft expiry. Per-aircraft publishes are min-throttled to one per second per hex to absorb bursty receiver outputs.
- **`adsb/summary/*`** publishes on change with throttling (max 1/sec across all summary topics). This is what casual HA users bind to — a stable handful of entities rather than one per aircraft.
- **`adsb/alert/<rule>/<hex>`** publishes on rule ENTER, cleared (null retained) on rule EXIT. After EXIT, a per-rule cooldown (default 60s) blocks re-ENTER for the same hex.

**HA discovery payloads** publish on the configured discovery prefix and create
**only the following entities** (locked — adding per-aircraft entities is an
explicit anti-goal):

- Per-receiver: `binary_sensor` for status, `sensor` for message rate, `sensor` for aircraft count.
- Service-wide: `sensor` for nearest aircraft (state = distance, attributes = full aircraft), `sensor` for total count, `sensor` per flag for count-by-flag.
- Per-alert-rule: `binary_sensor` that's `on` when the rule matches anything.

This gives users a working HA experience with zero template writing while
keeping HA's entity registry bounded.

**Discovery republish on connect.** On every successful broker connect (cold
start *and* reconnect after disconnect), the service republishes its full
discovery payload set. This is idempotent over retained topics and protects
against the case where the broker was restarted and lost its retained state —
HA would otherwise carry stale entities forever.

**Graceful shutdown protocol (locked).** On SIGTERM (or other clean exit),
the service distinguishes itself from a crash:

```python
async with aiomqtt.Client(host, will=Will(topic="adsb/status", payload=b"offline", retain=True)) as client:
    try:
        # ... main run loop ...
    finally:
        # Clean shutdown: publish offline non-retained, then exit context.
        # __aexit__ sends MQTT DISCONNECT before TCP close, which suppresses
        # the LWT will-message. HA observes a clean offline state.
        await client.publish("adsb/status", b"offline", retain=False)
        # context exit happens here automatically
```

If the process crashes (no clean `__aexit__`), the broker fires the LWT and HA
observes the retained `offline`. Distinguishing "user stopped service" from
"service crashed" is a load-bearing UX detail.

---

## Configuration schema

```yaml
# ha-airspace config.yaml

service:
  poll_interval_s: 1.0          # default; receivers can override
  http_timeout_s: 5.0
  log_level: "info"
  log_destination: "stdout"     # "stdout" (default — captured by HA add-on
                                # supervisor / journald / Docker) or "file"
                                # (rotation-capped, opt-in)

# Watchpoints replace the singular home_location: a list of named geographic
# points distance/bearing/alert rules can reference. Default config has one
# entry named "home" — backward-compatible feel.
watchpoints:
  - name: "home"
    lat: 30.3322
    lon: -97.9853
    elevation_m: 200            # optional; used for max_alt_agl_ft
  # Additional watchpoints (optional):
  # - name: "office"
  #   lat: ...
  #   lon: ...

mqtt:
  broker: "homeassistant.home.arpa"
  port: 1883
  username: !secret mqtt_user
  password: !secret mqtt_pass
  base_topic: "adsb"
  discovery_prefix: "homeassistant"
  discovery_enabled: true
  tls: false
  publish_aircraft_min_interval_s: 1.0   # per-hex throttle for adsb/aircraft/<hex>
  publish_summary_min_interval_s: 1.0    # global throttle for adsb/summary/*

# Optional Prometheus /metrics endpoint (Phase 1).
prometheus:
  enabled: false                # off by default
  bind: "127.0.0.1"             # localhost only by default
  port: 9090

databases:                       # Phase 2a
  cache_dir: "/data/db"
  refresh_interval_h: 168       # weekly
  sources:
    - name: "mictronics"
      url: "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
      enabled: true
    - name: "adsbexchange"
      url: "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz"
      enabled: true

journal:                         # Phase 2b — SQLite history (optional; omit
  path: "/data/journal.db"        #   = no persistence, first_seen resets on
                                  #   restart — the right call on a Pi SD card)
  # Writes are coalesced (buffered, flushed on a timer or event threshold),
  # never per poll. WAL + synchronous=NORMAL keep SD-card write amplification
  # low. Track summary rows (track_id, hex, first_seen) are kept forever;
  # event-history rows are pruned after retention_observations_days.
  retention_observations_days: 90
  write_coalesce_s: 30            # flush at least this often
  write_coalesce_events: 50       # flush early once this many records buffer
  # Persists durable first_seen plus flag/alert ENTER/EXIT events (pruned by
  # retention). The full-observation firehose, ~4M rows/day/receiver, stays out
  # of scope — this is a history ledger, not a track recorder.

photos:                          # Phase 2c — Planespotters photo enrichment
  enabled: false                 # off by default
  cache_ttl_days: 30
  inject_into: ["alerts"]        # alert payloads only; not the wildcard topic

receivers:
  - name: "home-1090"
    url: "http://piaware.home.arpa:8080/skyaware/data/aircraft.json"
    band: "1090"
    poll_interval_s: 1.0        # optional override
    location:                   # optional; auto-detected from receiver.json if absent
      lat: 30.3322
      lon: -97.9853
    auth:                       # optional
      type: "none"              # "none" | "basic" | "header"
    enabled: true

  - name: "home-978"
    url: "http://piaware.home.arpa:8080/skyaware978/data/aircraft.json"
    band: "978"

enrichment:                      # Phase 2a — flags (slice 1) + alerts (slice 3)
  flags:
    military:
      sources: ["mictronics:mil", "adsbexchange:mil"]
    interesting:
      sources: ["adsbexchange:interesting"]
    privacy:
      sources: ["adsbexchange:pia", "adsbexchange:ladd"]
    emergency_squawk:
      squawks: ["7500", "7600", "7700"]
    callsign_prefix:
      patterns: ["RCH", "SAM", "EVAC", "MEDEVAC", "LIFEGUARD", "N1"]
    type_code:
      types: ["B52", "C17", "C5M", "U2", "B1", "B2", "F22", "F35", "MQ9", "RC135"]
    rotorcraft:
      categories: ["A7"]                  # emitter category; no DB needed

  alerts:                        # Phase 2a slice 3
    cooldown_s: 60               # min seconds between EXIT and re-ENTER for
                                  # the same (rule, hex). Prevents thrashing
                                  # when a flag oscillates near a threshold.
    rules:
      - name: "military_close"
        match:
          flags: ["military"]              # OR within key
          max_distance_nm: 30              # AND across keys
          watchpoint: "home"               # which watchpoint distance is from
      - name: "interesting_close"
        match:
          flags: ["interesting", "privacy"]
          max_distance_nm: 15
          watchpoint: "home"
      - name: "emergency_anywhere"
        match:
          flags: ["emergency_squawk"]
      - name: "low_helicopter"
        match:
          category: ["A7"]                 # alert match keys can read raw fields
          max_alt_agl_ft: 2000             # directly, not only flags
          max_distance_nm: 10
          watchpoint: "home"

# publish.all_aircraft (max_distance filter for the wildcard topic) is a
# topic/MQTT concern; deferred and likely to move near `mqtt`. Not in 2a.
```

**Notes:**

- `max_alt_agl_ft` is approximated as MSL minus the watchpoint's `elevation_m` in v1. True AGL needs a DEM; document the limitation, especially for hilly terrain.
- Watchpoints are first-class. Each rule's `watchpoint` key picks which one its `max_distance_nm` is measured from. Default is `"home"` if omitted and there's exactly one watchpoint named `home`; otherwise required.
- Per-receiver `enabled: false` lets users keep config while disabling without deleting.
- Auth is generic — `type: header` lets users put arbitrary headers in (Cloudflare Access, custom tokens). Future-proofs.

**Note on schema versioning during pre-public phases (1–3):** the published MQTT
payload does NOT include a `schema_version` field during Phases 1–3. Daniel is
the only user during this period; breaking-change tolerance is high, and
locking a versioned contract before the field set has seen real usage would
risk pinning mistakes. Schema lock + `schema_version: 1` is a Phase 4
prerequisite alongside Docker / add-on packaging. Pydantic models still serve
as the canonical serializer through Phases 1–3 (consistency, type safety) but
their shape is allowed to evolve freely.

---

## Distribution

### HA add-on (primary)

Most HA users have HA. Add-ons are one-click install with a config UI, the Supervisor handles restarts/logs/updates, and they run on HA OS, HA Supervised, and HA Container with Supervisor.

Distribution mechanism: **add-on repository**. Users add a Git URL to their HA add-on store and the add-on appears. This is how Frigate, ESPHome, Mosquitto, etc. distribute. Not HACS — different system.

Repo layout:

```
ha-airspace-addon/
├── repository.yaml            # add-on store metadata
└── ha-airspace/
    ├── config.yaml            # add-on schema, options, ports
    ├── Dockerfile             # builds on ha-airspace Docker image
    ├── run.sh                 # supervisor entrypoint
    ├── README.md
    └── icon.png
```

The add-on Dockerfile is thin — it pulls the published `ha-airspace` Docker image and adds add-on-specific shims (s6-overlay if needed, config translation from add-on options to YAML config).

### Docker image

For HA Container users (no Supervisor), HA Core users, and anyone running it on a separate box.

Published to Docker Hub and GHCR. Multi-arch (amd64, arm64, armv7).

```bash
docker run -d \
  -v ./config.yaml:/config/config.yaml \
  -v ha-airspace-data:/data \
  ghcr.io/<user>/ha-airspace:latest
```

### Python package

For HA Core users on bare-metal Python, or for development. `pip install ha-airspace` plus a systemd unit.

### Why not HACS

HACS distributes things that run *inside* the HA Python process — custom integrations, Lovelace cards, themes. This service runs *outside* HA as a separate application, communicating via MQTT. That's add-on territory, not HACS.

If a custom Lovelace card emerges later (a tactical-display-style aircraft tracker, say), *that* could ship via HACS as a separate package. Independent of the enrichment service.

---

## Universality test

A good gut check — these four users should all work without code changes:

1. **Stock PiAware appliance, single 1090 receiver, HA on a NUC.** Add-on, point at appliance IP, done.
2. **Custom build, 1090 + 978 on same Pi, HA on same network.** Add-on, two receiver entries, done.
3. **Five receivers across two houses on Tailscale, HA on one of them.** Add-on, five receiver entries with Tailscale IPs.
4. **HA Core on Debian, dump1090 in Docker on the same box, no add-on support.** `pip install ha-airspace`, run as systemd service, point at `localhost:8080`.

If all four work without code changes, the universality goal is met.

---

## Implementation phases

### Phase 1: Core service (single receiver)

- `ReceiverSource` abstraction + `HttpJsonReceiver` implementation (pull-based `fetch()` with fail-fast semantics; consecutive-failure threshold tracked in `health()`)
- Basic `AircraftState` tracking (no merger yet — single source). State machine: NEW → ACTIVE → STALE → PURGED
- MQTT publisher with `aircraft/<hex>` and `summary/*` topics, graceful-shutdown protocol, discovery republish on connect
- HA discovery for summary + per-receiver + per-alert entities (NOT per-aircraft)
- Watchpoints as first-class config (default single entry "home")
- Optional Prometheus `/metrics` endpoint (off by default)
- Config loading and validation (Pydantic v2, strict mode, fail-fast on bad config)
- Default logging to stdout (HA add-on supervisor / journald / Docker captures)
- pip-installable (HA Container / HA Core / non-HA users; HAOS users wait for Phase 4 add-on)

**Done when:** a user can `pip install`, write a config with one receiver and one watchpoint, and see their nearest aircraft as a HA entity.

### Phase 2: Enrichment (split into 2a / 2b / 2c)

Each slice ships independently and gathers feedback before the next.

**Phase 2a: Rule engine.**
- Database loader (Mictronics + ADSBex) with weekly refresh, atomic dict swap, snapshot-on-enrich pattern
- Flag rule evaluation
- Alert rule evaluation with ENTER/EXIT state-transition detection
- Per-rule cooldown (default 60s) to suppress thrashing
- Per-alert MQTT topics + HA discovery binary_sensors

**Done when:** military aircraft appear on `adsb/alert/military_close/<hex>` with full DB metadata in the payload.

**Phase 2b: Durable history.**
- SQLite observation journal at `/data/journal.db` (WAL + synchronous=NORMAL, coalesced writes every 30s or 50 events, hand-rolled `PRAGMA user_version` migrations)
- Cadence: write only on flag-state changes and alert ENTER/EXIT — NOT every poll (SD-card friendliness)
- `first_seen` durable across restarts; history-aware alert rules ("first time this month")
- Mandatory retention on observation rows (default 90 days); aircraft summary rows kept forever

**Done when:** restarting the service preserves `first_seen` for every previously-observed hex.

**Phase 2c: Delight.**
- Planespotters photo enrichment (off by default, cached, fails-soft, photo URL injected into alert payloads only)
- Predictive `predicted_eta_to_home_s` and `predicted_closest_approach_nm` schema fields on `AircraftState` (always None during Phase 2c — implementation deferred to Phase 5 to avoid false-positive alerts on airport approach traffic)

**Done when:** alert payloads carry photo URLs (when configured) and `AircraftState` exposes the predictive fields as `None` placeholders.

### Phase 3: Multi-receiver merger

- Multiple `HttpJsonReceiver` instances running concurrently
- Merger with canonical position selection (NIC → NAC_p → seen_pos_age_s → RSSI → receiver name)
- Per-receiver health topics + discovery
- 1090 + 978 band merging

**Done when:** two receivers feeding the same hex produce one canonical aircraft with `seen_by: [...]` populated and the canonical pick is deterministic.

### Phase 4: Distribution

#### Phase 4 prerequisites (lock before starting Phase 4)

- **Artifacts to publish:**
  - HA add-on Docker image (one per arch: amd64, aarch64, armv7) via HA's official `image-builder` GitHub Action
  - Standalone Docker image (multi-arch manifest pointing to the same per-arch images) on `ghcr.io`
  - pip wheel + sdist on PyPI
- **Single Dockerfile** with `ARG BUILD_FROM=ghcr.io/home-assistant/{arch}-base-python:3.13`. The HA add-on `config.yaml` injects the arch-specific value; standalone Docker users supply their own (or use the same HA base).
- **Drop:** `armhf` and `i386` (HA itself is dropping them; supporting them is wasted CI time).
- **GitHub Actions workflows:**
  - `release-on-tag.yml` — triggers PyPI publish + multi-arch image build/push on git tag.
  - `ci.yml` — runs unit + integration tests on push (testcontainers + Mosquitto fixture).
- **Supply-chain checks (pre-Phase-4 audit):**
  - Mictronics / ADSBex DB hash verification (TOFU + warn-on-change).
  - Mictronics / ADSBex license review (ADSBex basic is CC-BY-NC; clarify redistribution implications for Docker image).
  - Pydantic v2 + `pydantic-core` armv7 wheel availability check on a real Pi 3 — fall back to source-build inside the Dockerfile if no wheel ships in time. HA's armv7 base image includes a Rust toolchain.
- **HAOS-vs-pip user guidance** in README: HAOS users install via the add-on store; pip is for HA Container, HA Core, CLI consumers, and Grafana folks. Do NOT tell HAOS users to `pip install`.
- **Schema lock:** before tagging the first public release, freeze the published MQTT payload schema and add a `schema_version: 1` field to the payload. Pydantic models become a stable contract.

#### Phase 4 execution

- HA add-on wrapper (s6-overlay + bashio, supervisor entrypoint, options-to-config translation)
- Add-on repository for HA's `repository.yaml` mechanism
- Standalone Docker image
- pip wheel
- README: install paths, config reference, MQTT topic reference, HA setup walkthrough
- `purge-discovery` CLI for clean uninstall (clears all retained discovery topics)

**Done when:** a user adds the add-on repo URL to HA, clicks install, fills in the config UI, and gets working entities without touching a terminal.

### Phase 5: Polish (post-MVP)

- True AGL via DEM (interesting for hilly terrain)
- Orbit detection (sustained turn rate + low forward progress)
- Predictive inbound alert *implementation* (schema reserved in Phase 2c). Requires airport-proximity exclusion zones, altitude floor/ceiling, ground-speed cruise filter, and orbit/turn detection to avoid false-positive alerts on every Southwest 737 on final into the nearest Class B airport.
- Custom Lovelace card (separate HACS package)
- Web UI for service status via HA add-on ingress (deferred to v1.1 / Phase 4.5)

---

## Open design questions

1. ~~**State persistence.**~~ **Resolved (Phase 2b):** SQLite journal at `/data/journal.db` persists `first_seen` and flag-state transitions. Restart preserves the durable history; in-flight `AircraftState` is rebuilt from the next poll.
2. ~~**Per-receiver position offsets.**~~ **Resolved (Phase 3):** pick canonical via NIC → NAC_p → seen_pos_age_s → RSSI → receiver name. Averaging is rejected; explicit-over-clever.
3. ~~**Rate limiting.**~~ **Resolved (Phase 1):** per-hex publish min-interval (default 1s) and global summary throttle (default 1s). HA receives no per-aircraft entities — only summary + per-alert — so HA's MQTT integration doesn't see the high-cardinality stream.
4. **Configuration reload.** SIGHUP to reload config without restart? Nice-to-have, not v1. Deferred.
5. ~~**Web UI.**~~ **Deferred to v1.1 / Phase 4.5** (TODOS). Single status page via HA add-on ingress. Useful for debugging, but separate frontend project; defer until MQTT path is proven over a quarter of real use.

---

## References

- Mictronics aircraft database: https://www.mictronics.de/aircraft-database/
- tar1090-db (CSV format): https://github.com/wiedehopf/tar1090-db
- ADSBexchange basic database: https://www.adsbexchange.com/database/contribute/
- readsb (active fork): https://github.com/adsb-related-code/readsb
- HA MQTT discovery: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
- HA add-on development: https://developers.home-assistant.io/docs/add-ons/

---

## Glossary

- **ADS-B** — Automatic Dependent Surveillance–Broadcast. The protocol aircraft use to broadcast position/identity on 1090 MHz (worldwide) and 978 MHz (US, low altitude only).
- **Hex / ICAO 24-bit address** — Unique 24-bit identifier per airframe, displayed as 6 hex chars. Doesn't change with callsign/registration changes (mostly).
- **Squawk** — 4-digit octal transponder code assigned by ATC. Special codes: 7500 (hijack), 7600 (radio failure), 7700 (general emergency).
- **Mode S category** — ADS-B emitter category: A1–A7 for fixed-wing by weight class, A7 specifically for rotorcraft, B6 for UAVs.
- **PIA** — Privacy ICAO Address. FAA program letting US operators rotate their hex code to obscure tracking.
- **LADD** — Limit Aircraft Data Displayed. FAA blocklist; aircraft on the list shouldn't be shown by FAA-source-based trackers.
- **Mictronics database** — Community-maintained mapping of hex → registration, type, operator, military flag. Used by readsb / tar1090.
- **MLAT** — Multilateration. Position calculation from time-of-arrival differences across multiple receivers, for aircraft without ADS-B.

---

# Appendix: Remote ID / drone source (shipped — Phase 3)

A companion project, [`dump3411`](https://github.com/ifnull/dump3411), detects
nearby drones via ASTM F3411 **Remote ID** (BLE + WiFi) on Linux. It surfaces
through this service's pipeline (watchpoint distance/bearing, nearest-entity,
MQTT + HA discovery) as a **separate project integrated by contract, not a code
merge.**

**Status: implemented in Phase 3.** `RemoteIdHttpReceiver` consumes the
`remoteid.json` feed (config `remoteid:`); drones merge as `band="remoteid"`
tracks keyed by UAS id, get distinct HA entities (`drone_count`,
`nearest_drone` with operator location) and `adsb/drone/<id>` topics, and skip
reference-DB lookup. The design notes below record the decisions as made.

## Integration shape

The detector serves its current detections as a JSON document over HTTP; this
service consumes it as just another source. The two repos share **zero code** —
only a documented feed contract (`FEED.md`, canonical copy in this repo).

- It is **not** `aircraft.json` and is **not** for dump1090-family tools.
  Drones are not manned aircraft; a literal `aircraft.json` clone would force a
  string UAS ID into the ICAO `hex` field (breaking Phase-2 reference-DB
  lookups) and would have nowhere to put operator location.
- We reuse dump1090's *envelope idioms* (`now`, `seen`/`seen_pos` as
  seconds-since, a polled ~1 Hz array) because the receiver/ingest plumbing
  already handles them — `seen→timestamp` conversion, staleness aging, purge.
- The *object schema* is purpose-built for Remote ID. See `FEED.md`.

## Key design decisions (free to make correctly now)

- **`band: "remoteid"`.** A drone is a third band alongside `1090`/`978`. It
  drops straight into the existing multi-band track model (`bands: [...]`) and
  the merger's per-band dedup. Remote ID `id` and ICAO hex are different
  namespaces and are **never** cross-matched.
- **Source-agnostic identity.** The track key is "whichever identity is present"
  (ICAO hex / TIS-B / Remote ID `id`), not a hardcoded ICAO assumption — same
  spirit as the `~`-prefixed non-ICAO flag for TIS-B/ADS-R. No reference-DB
  lookup is attempted for `remoteid` tracks. *This model decision is cheap to
  bake in during Phase 1's modeling even though the receiver is Phase 3.*
- **Operator location is a first-class, novel entity.** "Drone 400 m NE,
  operator 1.2 km S" — security-relevant and absent from ADS-B. Drones get their
  own HA discovery surface (drone count, nearest drone, operator location),
  distinct from the aircraft entities.
- **Native AGL for free.** Remote ID broadcasts height-above-takeoff directly —
  the AGL that Phase 5 wants for aircraft (and is hard to derive there) arrives
  in the feed.

## Phasing

- **Now (Phase 1):** keep the identity/observation model source-agnostic; leave
  a `# TODO(phase-3): RemoteIdHttpReceiver` hook where natural. Do **not** build
  the receiver — it needs the merger (Phase 3).
- **Phase 3:** add `RemoteIdHttpReceiver` (reuses the HTTP polling/backoff/
  `health()` plumbing) mapping the feed → the source-agnostic track model.
- **Producer side (`drone-aware-zero`):** an additive opt-in `--serve` mode
  (stdlib `http.server`, snapshot-only handler — the Zero W's single ARMv6 core
  is already saturated by decode). Tracked in that repo's `FEED.md`.
