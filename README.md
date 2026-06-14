# ha-airspace

[![ci](https://github.com/ifnull/ha-airspace/actions/workflows/ci.yml/badge.svg)](https://github.com/ifnull/ha-airspace/actions/workflows/ci.yml)

**Local-first airspace awareness for Home Assistant — manned aircraft *and*
drones.** `ha-airspace` polls `aircraft.json` from one or more local ADS-B
receivers (**dump1090**, **dump1090-fa**, **readsb**, **dump978** / UAT) and
Remote ID drone detections from **[dump3411](https://github.com/ifnull/dump3411)**
(ASTM F3411 over BLE + Wi-Fi), enriches them against reference databases, tags
military / interesting / emergency / privacy aircraft, and publishes everything
to **MQTT** with Home Assistant **MQTT discovery** — a working "nearest aircraft"
sensor (and drone alerts) with zero template writing.

Unlike FlightRadar24-based integrations, `ha-airspace` is **local-first**: it
reads your own receiver over HTTP, no cloud API, no account. And unlike every
other ADS-B Home Assistant project, it ingests **drone Remote ID** as a
first-class source. See [`DESIGN.md`](DESIGN.md) for architecture, the full
roadmap, and how it's positioned against the alternatives.

> **Why another one?** The popular HA flight integrations are cloud-API
> (FlightRadar24); the local-receiver ones don't merge multiple sources and
> none of them detect drones. `ha-airspace` is a standalone MQTT service with a
> multi-receiver merger (1090 + 978 + Remote ID), two-database enrichment, and
> a test suite — built to become infrastructure, not a weekend script.

> **Status:** Beta — released (HA add-on + multi-arch Docker image on GHCR),
> running against live receivers + a real broker. All planned phases are
> shipped: multi-receiver **1090 + 978** merge, **drone Remote ID** as a
> first-class source, reference-DB enrichment (Mictronics + ADSBexchange),
> declarative **flags** + stateful **alerts** (including history-aware
> "first-time/returning", **orbit/loiter** detection, and **predictive inbound**
> closest-approach), HA MQTT-discovery entities, an optional SQLite **journal**
> for durable history, Planespotters **photos** on alerts, and optional
> Prometheus metrics.
>
> **Deferred / not yet built** (none required): drone make/model FAA lookup,
> true AGL via DEM, a custom Lovelace "radar" card, a web status UI, and PyPI /
> armv7 / Docker-Hub distribution. See [`DESIGN.md`](DESIGN.md) for the full
> roadmap and rationale.

**Keywords:** Home Assistant, ADS-B, dump1090, dump978, readsb, tar1090,
PiAware, FlightAware, Remote ID, ASTM F3411, drone detection, dump3411, MQTT,
MQTT discovery, aircraft tracker, military aircraft, ADSBexchange, Mictronics,
1090 MHz, 978 MHz UAT, local-first, self-hosted.

---

## Requirements

- **Python 3.12+**
- A receiver exposing `aircraft.json` over HTTP (PiAware / dump1090-fa, readsb,
  tar1090, dump978-fa, …)
- An MQTT broker (Mosquitto, or the Home Assistant Mosquitto add-on)
- [`uv`](https://docs.astral.sh/uv/) for development (optional for end users)

---

## Quick start

**On Home Assistant OS**, install the **add-on** (see below) — that's the path
on HAOS. **Elsewhere**, run the **Docker image** (also below). For development
from a clone:

```bash
# 1. Install dependencies (from a clone)
uv sync

# 2. Create your config from the example
cp config.example.yaml config.yaml
$EDITOR config.yaml            # set your receiver URL, broker, and watchpoint

# 3. Run
uv run ha-airspace --config config.yaml
#   — or: uv run python -m ha_airspace --config config.yaml
```

> A PyPI release (`pip install ha-airspace`) is planned but not published yet;
> for now use the add-on, the Docker image, or a clone.

On startup the service connects to the broker, publishes the HA discovery
payloads, and begins polling. Within a few seconds Home Assistant shows the
entities below — no YAML templates required.

Bad config fails fast: a missing file or invalid schema prints a clear error
(with the offending field path) and exits non-zero before anything connects.

### Run with Docker

The image is multi-arch (amd64 / arm64) and reads its config from `/config`
with persistent state in `/data`:

```bash
docker run -d --name ha-airspace \
  --restart unless-stopped \
  -v ./config.yaml:/config/config.yaml:ro \
  -v ha-airspace-data:/data \
  ghcr.io/ifnull/ha-airspace:latest
```

`--restart unless-stopped` makes Docker bring it back on boot and after a
crash. On Home Assistant OS the add-on handles this for you (boot: auto).

### Run as a Home Assistant add-on

Add this repo as an add-on store (Settings → Add-ons → Add-on Store → ⋮ →
**Repositories**): `https://github.com/ifnull/ha-airspace`, then install
**ha-airspace**. Receivers, watchpoints, and MQTT are configured from the
add-on UI; with the Mosquitto add-on installed the MQTT connection auto-fills.
Full reference: [`addon/DOCS.md`](addon/DOCS.md).

---

## Configuration

[`config.example.yaml`](config.example.yaml) is the annotated reference. The
only required sections are `watchpoints`, `mqtt`, and `receivers`; everything
else has sensible defaults. Validation is strict — unknown keys are rejected
with a helpful error rather than silently ignored.

Minimal config:

```yaml
watchpoints:
  - name: home
    lat: 30.3322
    lon: -97.9853

mqtt:
  broker: homeassistant.local

receivers:
  - name: home-1090
    url: http://piaware.local:8080/skyaware/data/aircraft.json
    band: "1090"
```

- **`watchpoints`** — named locations distance/bearing and "nearest" are
  measured from. The one named `home` is the primary (else the first listed).
- **`receivers`** — one or more `aircraft.json` sources. `band` is required
  (`1090` | `978`); the receiver's own location is auto-discovered from its
  `receiver.json` when present.
- **`mqtt`** — only `broker` is required. Per-hex and summary publish rates are
  throttled (defaults 1/s) so HA never sees a high-cardinality stream.

---

## Home Assistant entities

Created automatically via MQTT discovery (no per-aircraft entities — that would
overwhelm HA's registry; power users subscribe to the wildcard topic instead):

| Entity | Source |
|---|---|
| `sensor.airspace_aircraft_count` | total aircraft currently tracked |
| `sensor.airspace_nearest_aircraft` | distance to the closest aircraft; full details in its attributes |
| `sensor.airspace_drone_count` | drones currently tracked (with a Remote ID feed) |
| `sensor.airspace_nearest_drone` | nearest drone; operator location in its attributes |
| `binary_sensor.airspace_alert_<rule>` | on while any aircraft matches that alert rule |
| `binary_sensor.airspace_receiver_<name>_status` | per-receiver connectivity |
| `sensor.airspace_receiver_<name>_aircraft_count` | per-receiver aircraft count |
| `sensor.airspace_receiver_<name>_message_rate` | per-receiver message rate |

HA derives these entity_ids from the device name ("Airspace") + the entity name,
so they're `sensor.airspace_<thing>`. (A receiver named `home-1090` slugifies to
`home_1090`.) All carry an availability binding to the service's `airspace/status`
topic, so they go *unavailable* when the service stops or crashes rather than
showing stale values.

A ready-to-adapt Lovelace dashboard (map of nearest traffic, overview glance,
nearest-aircraft detail, receiver health, alert badges) built from these
entities with stock cards is in [`docs/dashboard.example.yaml`](docs/dashboard.example.yaml).

---

## MQTT topics

Published under `base_topic` (default `airspace`):

```
airspace/status                          online | offline (LWT + graceful shutdown)
airspace/summary/count                   total aircraft
airspace/summary/nearest                 JSON: closest aircraft (full state)
airspace/summary/count_by_flag           JSON: counts per flag ({} when no flag rules configured)
airspace/aircraft/<hex>                  JSON: per-aircraft state (wildcard; not an HA entity)
airspace/receiver/<name>/status          online | unhealthy | offline
airspace/receiver/<name>/stats           JSON: count, message rate, health
airspace/receiver/<name>/location        JSON: receiver lat/lon
```

State-bearing topics are retained, so a freshly started consumer sees current
state immediately. Aircraft that leave coverage have their retained topic
cleared (no zombie aircraft lingering in HA).

---

## Optional: Prometheus metrics

Off by default; localhost-bound when enabled. Set `prometheus.enabled: true` in
config to expose `/metrics` (receiver poll outcomes, MQTT publish/drop/reconnect
counters, active-aircraft gauge). Bind to `0.0.0.0` only if you want it on the
LAN — the endpoint is unauthenticated.

---

## Development

```bash
uv sync                        # install deps + dev tools
uv run pytest                  # unit tests (no network, no broker)
uv run pytest -m integration   # integration tests (requires Docker — Mosquitto)
uv run mypy src                # type check (strict)
uv run ruff check src tests    # lint
uv run ruff format --check .   # format check
```

Conventions live in [`CLAUDE.md`](CLAUDE.md); architecture and the phase
breakdown in [`DESIGN.md`](DESIGN.md). Integration tests spin up a real
Mosquitto container via testcontainers and are skipped unless you pass
`-m integration`.

---

## License

MIT. See the `license` field in [`pyproject.toml`](pyproject.toml).
