# adsb-enrich — Design Document

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
    band: str = "1090"                # "1090" | "978"


class ReceiverSource:
    """Abstract base. Concrete implementations:
      - HttpJsonReceiver  (dump1090-fa, dump1090-mutability, readsb, dump978-fa)
      - FileReceiver      (replay a recorded aircraft.json for testing)
      - TestReceiver      (synthetic data for unit tests)
    """
    name: str
    band: str

    async def location(self) -> Optional[ReceiverLocation]:
        """One-shot fetch of receiver location. Cached by caller."""
        ...

    async def observations(self) -> AsyncIterator[list[AircraftObservation]]:
        """Yields a fresh list every poll cycle. Handles its own retry/backoff.

        Yields an empty list on transient failure rather than raising —
        the merger should keep running if one receiver is flaky.
        """
        ...

    async def health(self) -> dict:
        """Status for diagnostics. Required keys:
          online: bool
          last_success: datetime
          aircraft_count: int
          messages_per_sec: float
        Implementations may include extras.
        """
        ...
```

**Key design decisions:**

- **`AircraftObservation` is per-receiver, not deduplicated.** The merger downstream produces canonical aircraft from N observations. Keeping observations separate preserves "which receivers saw this" data and lets us pick the best position when receivers disagree.
- **Units are baked into field names.** `alt_baro_ft`, `ground_speed_kt`, `vertical_rate_fpm`. dump1090 already uses these units; making them explicit prevents future confusion.
- **`observed_at` is when *we* polled, not when the receiver saw it.** dump1090's `now` field is the receiver's clock and may be skewed. We trust our own clock for staleness math; the receiver's `seen` deltas tell us how stale relative to it.
- **`band` is first-class.** 1090 vs 978 affects merging logic and is genuinely user-visible.
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
- Refresh must not block the main event loop. Run in a thread executor.
- Cache to disk so service can come up without network access.
- Never let a failed refresh wipe an existing in-memory copy.

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
    distance_nm: Optional[float] = None
    bearing_deg: Optional[float] = None
```

**Canonical position selection.** When the same hex appears on multiple receivers in the same poll cycle, pick canonical position by:

1. Prefer observations with `seen_pos_age_s` < 5 (fresh position)
2. Among those, prefer highest `rssi_dbfs` (strongest signal)
3. Among those, prefer the closer receiver (better geometry)
4. Tiebreak: deterministic (alphabetical receiver name) so behavior is stable

**Aircraft expiry.** Drop from state when no receiver has seen the hex for `expiry_s` (default 60s). On expiry, publish a "removed" event so consumers can clean up.

**1090 + 978 merging.** Same hex on both bands is the same aircraft; merge into one state with `bands = {"1090", "978"}`. Position can come from either; band-specific quality fields (rssi, message rate) stay per-observation.

### 4. Enrichment pipeline

After the merger updates `AircraftState`, run enrichment:

1. **DB join.** Look up hex in Mictronics + ADSBex, populate `db_metadata` with merged fields (registration, type code, operator, military flag, etc.). Conflicts resolved by source priority (ADSBex's `mil` flag wins over Mictronics if they disagree, since ADSBex updates more frequently for military).
2. **Geometry.** Compute distance/bearing from a configured "home" point (defaults to first receiver's location). Haversine is fine; vincenty is overkill.
3. **Flag evaluation.** Apply each flag rule from config; populate `state.flags`.
4. **Alert evaluation.** Apply each alert rule; produce alert events for state transitions (rule entered/exited).

**Flag rules are declarative.** Adding a new flag is a config change, not a code change:

```yaml
flags:
  military:
    sources: ["mictronics:mil", "adsbexchange:mil"]   # OR logic
  callsign_prefix:
    patterns: ["RCH", "SAM", "EVAC", "MEDEVAC"]
```

**Alert rules compose flags:**

```yaml
alerts:
  rules:
    - name: "military_close"
      match:
        flags: ["military"]
        max_distance_nm: 30
```

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

- **`adsb/aircraft/<hex>`** publishes on every state change. High volume; users who don't want it can ignore the wildcard topic. Retained so late subscribers see current state. Use `null` payload to clear on aircraft expiry.
- **`adsb/summary/*`** publishes on change with throttling (max 1/sec). This is what casual HA users will bind to — stable handful of entities rather than one per aircraft.
- **`adsb/alert/<rule>/<hex>`** publishes on rule entry, cleared (null retained) on rule exit. Lets HA automations trigger on retain → null transitions for "alert ended" logic.

**HA discovery payloads** publish on the configured discovery prefix and create:

- Per-receiver: `binary_sensor` for status, `sensor` for message rate, `sensor` for aircraft count.
- Service-wide: `sensor` for nearest aircraft (state = distance, attributes = full aircraft), `sensor` for total count, `sensor` per flag for count-by-flag.
- Per-alert-rule: `binary_sensor` that's `on` when the rule matches anything.

This gives users a working HA experience with zero template writing.

---

## Configuration schema

```yaml
# adsb-enrich config.yaml

service:
  poll_interval_s: 1.0          # default; receivers can override
  http_timeout_s: 5.0
  log_level: "info"
  home_location:                # for distance/bearing calculation
    lat: 30.3322                # falls back to first receiver if absent
    lon: -97.9853

mqtt:
  broker: "homeassistant.home.arpa"
  port: 1883
  username: !secret mqtt_user
  password: !secret mqtt_pass
  base_topic: "adsb"
  discovery_prefix: "homeassistant"
  discovery_enabled: true
  tls: false

databases:
  cache_dir: "/data/db"
  refresh_interval_h: 168       # weekly
  sources:
    - name: "mictronics"
      url: "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
      enabled: true
    - name: "adsbexchange"
      url: "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz"
      enabled: true

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

enrichment:
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

publish:
  all_aircraft:
    enabled: true
    max_distance_nm: 100

  alerts:
    enabled: true
    rules:
      - name: "military_close"
        match:
          flags: ["military"]
          max_distance_nm: 30
      - name: "interesting_close"
        match:
          flags: ["interesting", "privacy"]
          max_distance_nm: 15
      - name: "emergency_anywhere"
        match:
          flags: ["emergency_squawk"]
      - name: "low_helicopter"
        match:
          category: ["A7"]
          max_alt_agl_ft: 2000
          max_distance_nm: 10
```

**Notes:**

- `max_alt_agl_ft` is approximated as MSL minus receiver elevation in v1. True AGL needs a DEM; document the limitation, especially for hilly terrain.
- `home_location` is separate from any receiver — users with multiple sites want distance from their actual home, not from a receiver.
- Per-receiver `enabled: false` lets users keep config while disabling without deleting.
- Auth is generic — `type: header` lets users put arbitrary headers in (Cloudflare Access, custom tokens). Future-proofs.

---

## Distribution

### HA add-on (primary)

Most HA users have HA. Add-ons are one-click install with a config UI, the Supervisor handles restarts/logs/updates, and they run on HA OS, HA Supervised, and HA Container with Supervisor.

Distribution mechanism: **add-on repository**. Users add a Git URL to their HA add-on store and the add-on appears. This is how Frigate, ESPHome, Mosquitto, etc. distribute. Not HACS — different system.

Repo layout:

```
adsb-enrich-addon/
├── repository.yaml            # add-on store metadata
└── adsb-enrich/
    ├── config.yaml            # add-on schema, options, ports
    ├── Dockerfile             # builds on adsb-enrich Docker image
    ├── run.sh                 # supervisor entrypoint
    ├── README.md
    └── icon.png
```

The add-on Dockerfile is thin — it pulls the published `adsb-enrich` Docker image and adds add-on-specific shims (s6-overlay if needed, config translation from add-on options to YAML config).

### Docker image

For HA Container users (no Supervisor), HA Core users, and anyone running it on a separate box.

Published to Docker Hub and GHCR. Multi-arch (amd64, arm64, armv7).

```bash
docker run -d \
  -v ./config.yaml:/config/config.yaml \
  -v adsb-enrich-data:/data \
  ghcr.io/<user>/adsb-enrich:latest
```

### Python package

For HA Core users on bare-metal Python, or for development. `pip install adsb-enrich` plus a systemd unit.

### Why not HACS

HACS distributes things that run *inside* the HA Python process — custom integrations, Lovelace cards, themes. This service runs *outside* HA as a separate application, communicating via MQTT. That's add-on territory, not HACS.

If a custom Lovelace card emerges later (a tactical-display-style aircraft tracker, say), *that* could ship via HACS as a separate package. Independent of the enrichment service.

---

## Universality test

A good gut check — these four users should all work without code changes:

1. **Stock PiAware appliance, single 1090 receiver, HA on a NUC.** Add-on, point at appliance IP, done.
2. **Custom build, 1090 + 978 on same Pi, HA on same network.** Add-on, two receiver entries, done.
3. **Five receivers across two houses on Tailscale, HA on one of them.** Add-on, five receiver entries with Tailscale IPs.
4. **HA Core on Debian, dump1090 in Docker on the same box, no add-on support.** `pip install adsb-enrich`, run as systemd service, point at `localhost:8080`.

If all four work without code changes, the universality goal is met.

---

## Implementation phases

### Phase 1: Core service (single receiver)

- `ReceiverSource` abstraction + `HttpJsonReceiver` implementation
- Basic `AircraftState` tracking (no merger yet — single source)
- MQTT publisher with `aircraft/<hex>` and `summary/*` topics
- HA discovery for summary entities
- Config loading and validation
- systemd-friendly logging
- pip-installable

**Done when:** a user can `pip install`, write a config with one receiver, and see their nearest aircraft as a HA entity.

### Phase 2: Enrichment

- Database loader (Mictronics + ADSBex) with weekly refresh
- Flag rule evaluation
- Alert rule evaluation with state-transition detection
- Per-flag MQTT topics + discovery

**Done when:** military aircraft appear on `adsb/alert/military_close/<hex>` with full DB metadata in the payload.

### Phase 3: Multi-receiver merger

- Multiple `HttpJsonReceiver` instances running concurrently
- Merger with canonical position selection
- Per-receiver health topics + discovery
- 1090 + 978 band merging

**Done when:** two receivers feeding the same hex produce one canonical aircraft with `seen_by: [...]` populated.

### Phase 4: Distribution

- Multi-arch Docker image
- HA add-on wrapper
- Add-on repository
- Documentation site / README

**Done when:** a user adds the add-on repo URL to HA, clicks install, fills in the config UI, and gets working entities without touching a terminal.

### Phase 5: Polish (post-MVP)

- True AGL via DEM (interesting for hilly terrain)
- Orbit detection (sustained turn rate + low forward progress)
- Predictive alerts (aircraft heading toward a configured point)
- Historical recording (sqlite of every aircraft seen)
- Photo lookup via Planespotters API
- Custom Lovelace card (separate HACS package)

---

## Open design questions

1. **State persistence.** Should `AircraftState` survive service restarts? Probably not for v1 (aircraft turnover is fast, restart is rare), but worth confirming.
2. **Per-receiver position offsets.** If two receivers report slightly different positions for the same aircraft (multilateration disagreement), do we average or pick one? Current design picks one (canonical). Averaging is more accurate but harder to explain.
3. **Rate limiting.** Do we need to throttle MQTT publishes per-aircraft? At 1Hz poll × 50 aircraft × multiple receivers, we're at a few hundred messages/sec. Mosquitto handles this fine; HA's MQTT integration may not love it. Worth measuring.
4. **Configuration reload.** SIGHUP to reload config without restart? Nice-to-have, not v1.
5. **Web UI.** Add-ons can have a web UI via ingress. A simple status page (receivers online, current aircraft count, recent alerts) would be useful for debugging. Probably v2.

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
