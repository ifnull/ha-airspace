# ha-airspace

Multi-source ADS-B + drone Remote ID enrichment to MQTT for Home Assistant.
This add-on runs the `ha-airspace` service inside HA: it polls one or more
ADS-B receivers (and optional drone Remote ID feeds), enriches the data, and
publishes it to MQTT with HA discovery, so aircraft and drones show up as
entities without a custom integration.

## Prerequisites

- An MQTT broker. The [Mosquitto broker add-on][mosquitto] is the easy path —
  install it and this add-on auto-fills the connection (leave the MQTT options
  blank). For an external broker, fill in `mqtt_broker` etc.
- At least one ADS-B receiver exposing `aircraft.json` over HTTP (dump1090-fa /
  PiAware, reairspace/tar1090, dump978-fa).

## Configuration

### Required

- **receivers** — one entry per receiver:
  - `name`: stable id (used in MQTT topics/metrics; renaming breaks dashboards).
  - `url`: the `aircraft.json` URL, e.g.
    `http://piaware.local:8080/skyaware/data/aircraft.json`.
  - `band`: `1090` or `978`. No default — mislabeling a 978 receiver as 1090 is
    a silent footgun.
  - `username` / `password`: optional HTTP basic auth.
- **watchpoints** — named locations distance/bearing and "nearest" are measured
  from. The one named `home` is the default reference. `lat`/`lon` required;
  `elevation_m` optional (reserved for AGL).

### MQTT

Leave `mqtt_broker` blank to use the HA Mosquitto add-on automatically. To
target another broker, set `mqtt_broker`, `mqtt_port`, `mqtt_username`,
`mqtt_password`. `mqtt_base_topic` defaults to `airspace`.

### Enrichment & detection (toggles — no YAML needed)

These turn on the value-add features directly from the UI; the add-on builds
the underlying flag/alert/database config for you.

- **enable_databases** — download the Mictronics + ADSBexchange reference DBs
  (registration, type, operator, and the military / privacy markers). On by
  default; cached under `/data/db`. Required for the `military` and
  `interesting` flags to actually match.
- **enable_emergency** — tag aircraft squawking 7500/7600/7700 (`emergency`
  flag). No database needed.
- **enable_military** — tag military aircraft (`military` flag; needs
  `enable_databases`).
- **enable_interesting** — tag privacy / blocklist aircraft — PIA + LADD
  (`interesting` flag; needs `enable_databases`).
- **enable_alerts** / **alert_distance_nm** — also raise an alert for each
  enabled tag: `emergency` fires anywhere; the rest fire within
  `alert_distance_nm` (default 30 nm) of your first watchpoint. Each becomes a
  `binary_sensor.airspace_alert_<rule>`.
- **enable_orbit** / **orbit_min_turn_deg** — flag circling/loitering tracks
  (`orbiting`); with alerts on, also raises `orbiting_nearby`.
- **enable_photos** — attach a Planespotters photo URL to alert payloads.

### Optional

- **remoteid** — drone Remote ID feeds (dump3411), `name` + `url`.
- **enable_journal** — persist `first_seen` and flag/alert history to
  `/data/journal.db` so it survives restarts (on by default; writes are
  coalesced and SD-card-friendly).
- **enable_metrics** / **metrics_port** — expose Prometheus `/metrics`. Map the
  port in the add-on Network panel to reach it.

### Advanced: extra_config

You should not need this for normal use — the toggles above cover the common
features. `extra_config` is raw YAML **deep-merged over** the generated config,
for **custom** flag/alert rules the toggles don't express, or to **override** a
generated value (e.g. a custom MQTT TLS setting, or a hand-tuned alert rule).
A bad value fails fast in the add-on log with the offending field path.

#### extra_config example (custom rules + a history-aware alert)

```yaml
enrichment:
  flags:
    emergency:
      squawks: ["7500", "7600", "7700"]
    military:
      sources: ["adsbexchange:mil"]   # needs the databases section below
  alerts:
    rules:
      - name: emergency_any
        match:
          flags: ["emergency"]
      - name: new_military           # history-aware (needs enable_journal)
        match:
          flags: ["military"]
          unseen_for_days: 30
databases:                            # sources is a LIST; each needs name + url
  sources:
    - name: mictronics                # name selects the parser
      url: https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz
      enabled: true
    - name: adsbexchange
      url: https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz
      enabled: true
orbit:                                # Phase 5 — derived "orbiting" flag
  enabled: true
photos:                               # Phase 2c — photo URL on alert payloads
  enabled: true
```

See the project README and `config.example.yaml` for the full schema.

## How it maps to config

The add-on translates these options into the service's native YAML at
`/data/config.yaml` on each start. A bad value surfaces as a fail-fast config
error in the add-on log (exit code 2) with the offending field path.

[mosquitto]: https://github.com/home-assistant/addons/tree/master/mosquitto
