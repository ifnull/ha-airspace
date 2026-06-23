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
- **enable_drone_registry** — look up each drone's broadcast serial against the
  FAA UAS registry to add make / model / status to the drone payload. Only
  resolves serial-type Remote IDs and only does anything with a **remoteid**
  feed configured; cached in memory, fails soft. Compliance data, not operator
  identity (operator *location* still comes from the broadcast).
- **enable_spoof_detection** — flag drones whose Remote ID broadcast looks
  fabricated (`spoof_suspect`): a malformed/placeholder serial, or the same
  free-text self-ID shared across multiple distinct serials. Behavioral, not
  identity-based — RID has no authentication and a spoofer can replay genuine
  serials. Only acts on **remoteid** tracks; with alerts on, a `spoof_suspect`
  rule fires like any other flag.

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

**Merge behavior — read this before mixing toggles and `extra_config`:**

- **Nested mappings merge.** `extra_config`'s `enrichment.flags` is added to the
  toggle-generated flags; `orbit: { min_turn_deg: 270 }` overrides just that key.
- **Lists replace wholesale — they do not append.** If you set
  `enrichment.alerts.rules` in `extra_config`, it **replaces every**
  toggle-generated alert rule. So if you want a custom rule *plus* the automatic
  ones, list them all in `extra_config` (the example below does this). The same
  applies to `databases.sources` and `receivers`.
- **Don't re-declare what a toggle already produces.** Turning a toggle on and
  also setting the same section in `extra_config` is redundant and a common
  source of confusion — prefer the toggle, and reserve `extra_config` for what
  toggles can't express (e.g. `unseen_for_days` rules, pattern/type/category
  flags, custom MQTT TLS).
- **Indentation must be exact** — a stray space fails the YAML parse at start-up.
  And **paste over the whole field**: select-all (Ctrl-A) inside the box first,
  then paste. The editor appends rather than replaces, so pasting on top of
  existing text silently duplicates your `rules` list (a duplicated rule with a
  missing `match` is the usual symptom).

#### extra_config example (additive flags + a full custom rules list)

This adds only what the toggles can't express — pattern / type / category flags
(no database needed; they read straight from the broadcast) and a history-aware
rule — and does **not** re-declare `databases`, `orbit`, or `photos` (those stay
on via their toggles). Because `alerts.rules` replaces wholesale, it lists every
rule to keep, including the ones the toggles would have generated.

```yaml
enrichment:
  flags:
    # Added to the toggle flags (military / interesting / emergency).
    mil_callsign:
      patterns: ["RCH", "SAM", "EVAC", "MEDEVAC", "LIFEGUARD"]  # callsign prefix
    heavy_mil:
      types: ["B52", "C17", "C5M", "U2", "RC135"]               # ICAO type code
    rotorcraft:
      categories: ["A7"]                                        # emitter category
  alerts:
    # Replaces the toggle rules, so it repeats the ones to keep (incl.
    # orbiting_nearby when enable_orbit is on) and adds the custom ones.
    rules:
      - name: military_close
        match: { flags: ["military"], max_distance_nm: 30 }
      - name: interesting_nearby
        match: { flags: ["interesting"], max_distance_nm: 30 }
      - name: emergency_anywhere
        match: { flags: ["emergency"] }
      - name: orbiting_nearby
        match: { flags: ["orbiting"], max_distance_nm: 30 }
      - name: new_military           # history-aware (needs enable_journal)
        match: { flags: ["military"], unseen_for_days: 30 }
      - name: mil_callsign_close
        match: { flags: ["mil_callsign"], max_distance_nm: 50 }
      - name: helicopter_low
        match: { flags: ["rotorcraft"], max_distance_nm: 10 }
```

See the project README and `config.example.yaml` for the full schema.

## How it maps to config

The add-on translates these options into the service's native YAML at
`/data/config.yaml` on each start. A bad value surfaces as a fail-fast config
error in the add-on log (exit code 2) with the offending field path.

[mosquitto]: https://github.com/home-assistant/addons/tree/master/mosquitto
