# adsb-enrich

A multi-source ADS-B enrichment service. It polls `aircraft.json` from one or
more receivers (dump1090-fa, readsb, dump978-fa), tracks aircraft state,
computes distance and bearing from your locations, and publishes to MQTT with
Home Assistant discovery — so you get a working "nearest aircraft" sensor with
zero template writing.

It is **not** a feeder, not a replacement for dump1090, and not a website. It
consumes a receiver's HTTP `aircraft.json` and turns it into clean MQTT topics +
HA entities. See [`DESIGN.md`](DESIGN.md) for architecture and the full roadmap.

> **Status:** Phase 1 (single receiver, no reference DBs yet). Pip-installable
> and runnable today; verified end to end against live receivers and a real
> MQTT broker. Reference-database enrichment, flag/alert rules, and the
> multi-receiver merger land in later phases.

---

## Requirements

- **Python 3.12+**
- A receiver exposing `aircraft.json` over HTTP (PiAware / dump1090-fa, readsb,
  tar1090, dump978-fa, …)
- An MQTT broker (Mosquitto, or the Home Assistant Mosquitto add-on)
- [`uv`](https://docs.astral.sh/uv/) for development (optional for end users)

---

## Quick start

```bash
# 1. Install (pip, or uv for development)
pip install adsb-enrich        # end users
#   — or, from a clone, for development:
uv sync

# 2. Create your config from the example
cp config.example.yaml config.yaml
$EDITOR config.yaml            # set your receiver URL, broker, and watchpoint

# 3. Run
adsb-enrich --config config.yaml
#   — or: python -m adsb_enrich --config config.yaml
#   — or, from a clone: uv run adsb-enrich --config config.yaml
```

On startup the service connects to the broker, publishes the HA discovery
payloads, and begins polling. Within a few seconds Home Assistant shows the
entities below — no YAML templates required.

Bad config fails fast: a missing file or invalid schema prints a clear error
(with the offending field path) and exits non-zero before anything connects.

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
| `sensor.adsb_count` | total aircraft currently tracked |
| `sensor.adsb_nearest` | distance to the closest aircraft; full details in its attributes |
| `binary_sensor.adsb_receiver_<name>_status` | per-receiver connectivity |
| `sensor.adsb_receiver_<name>_aircraft_count` | per-receiver aircraft count |
| `sensor.adsb_receiver_<name>_messages_per_sec` | per-receiver message rate |

All carry an availability binding to the service's `adsb/status` topic, so they
go *unavailable* when the service stops or crashes rather than showing stale
values.

---

## MQTT topics

Published under `base_topic` (default `adsb`):

```
adsb/status                          online | offline (LWT + graceful shutdown)
adsb/summary/count                   total aircraft
adsb/summary/nearest                 JSON: closest aircraft (full state)
adsb/summary/count_by_flag           JSON: counts per flag ({} until Phase 2 rules land)
adsb/aircraft/<hex>                  JSON: per-aircraft state (wildcard; not an HA entity)
adsb/receiver/<name>/status          online | unhealthy | offline
adsb/receiver/<name>/stats           JSON: count, message rate, health
adsb/receiver/<name>/location        JSON: receiver lat/lon
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
