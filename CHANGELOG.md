# Changelog

All notable changes to `ha-airspace` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[Semantic Versioning](https://semver.org/).

The **published MQTT payload contract** is versioned separately via
`schema_version` (see [README → Stable interfaces](README.md#stable-interfaces)).
Additive, optional payload fields are backwards-compatible and do **not** bump
the major version; a removed/renamed/retyped field does.

## [1.1.1] — 2026-08-30
### Added
- `faulthandler` is enabled at startup, and `SIGUSR1` dumps every thread's
  stack (`kill -USR1 <pid>`). A hard crash in a C extension and a kernel
  SIGKILL previously left identical evidence — an add-on log that just starts
  over with no shutdown line, no traceback, nothing. Now only the SIGKILL is
  silent, which makes the absence of a dump a useful signal in itself
  (check `ha host logs | grep -i oom`).

### Changed
- Receivers: `receiver_fetch_failed` now backs off instead of logging every
  poll. Every failure up to the unhealthy threshold is still logged (that is
  the window that diagnoses a blip); after that the interval doubles up to one
  line per ~1200 failures, each carrying `suppressed_since_last`. A receiver
  coming back now logs `receiver_recovered` with the failure count it endured —
  previously recovery was silent. A single misconfigured receiver URL was
  otherwise good for 22159 identical warning lines in 18 hours, all of them
  journald writes on an SD card.

### Fixed
- Reference DBs: the loader no longer holds three copies of the ~620k-row
  merged database at once. Each source was parsed into its own dict, copied
  entry-by-entry into a third, and the next source was then parsed while both
  were still live — ~610 MB anon-RSS at peak on aarch64. On a 2 GB Raspberry Pi
  that is enough to trigger a **global** OOM kill (the add-on is the largest
  process, and Supervisor sets the container `mem_limit` to total system RAM,
  so the cgroup limit never trips first): the service was killed with SIGKILL
  ~10-60s after `db_refreshed`, restarted by Supervisor, and killed again, with
  no shutdown or crash log to explain it. Sources now parse in ascending
  priority directly into one shared dict, and the repeated string fields
  (`type`, `model`, `ownop`) reuse one object per distinct value. Peak drops to
  ~240 MB. Merge precedence (ADSBex over Mictronics, per-key) is unchanged, and
  a mid-stream source failure still leaves the previous good copy in place.

## [1.1.0] — 2026-08-22
### Added
- `DroneInfo.operator_location_type` / `DronePayload.operator_location_type`
  (`airspace/drone/<id>`, `sensor.airspace_nearest_drone`): dump3411 now
  decodes what a System message's operator `lat`/`lon` actually represents —
  `takeoff` (the drone's own launch point) | `live_gnss` | `fixed`. Some
  transmitters (reported: Potensic RID-916) toggle this across messages, so
  operator coordinates aren't always a live operator fix. Additive field, no
  `schema_version` bump. `FEED.md` and the example automations/dashboard
  updated to mirror dump3411's own dashboard fix: annotate a takeoff-point
  reading instead of labelling it "operator located" like a live fix.
- Example `drone_nearby` alert rule (`config.example.yaml`,
  `docs/automations.example.yaml`): `drone_conflict`'s predictive
  `max_closest_approach_nm`/`within_eta_s` gate never fires for a
  hovering/loitering drone (no sustained closing vector → no projected ETA);
  `drone_nearby` is a pure-presence companion rule for that case.

### Fixed
- MQTT: `airspace/summary/nearest` and `nearest_drone` no longer republish an
  empty-retained payload on every tick while the airspace is empty — only on
  the transition into empty. HA's MQTT integration tries to JSON-decode every
  `state_topic` payload before running `value_template` (both sensors read
  `value_json`), so the previous behavior logged "Erroneous JSON" + a template
  warning once per publish interval, continuously, for as long as the airspace
  was empty — most of the time for a lot of installs. Also resets the new
  latch on MQTT reconnect, matching the existing `_last_receiver_status`
  precedent, so a broker that loses retained state still gets the clearing
  payload re-sent. (#56, external contribution — thanks richmobile!)
- `docs/dashboard.example.yaml`: the top-of-file note only mentioned the
  drone-operator marker as needing a manual template sensor, implying the
  aircraft/drone map markers worked out of the box via discovery. All three
  (`_aircraft_map`, `_drone_map`, `_drone_operator_map`) require the
  `configuration.yaml` template block — omitting it leaves the Map card
  showing nothing but the home zone.

## [1.0.0] — 2026-06-23
First stable release. The 0.x series built and proved the full pipeline against
live receivers + a real broker over a quarter of use; 1.0.0 marks the published
**MQTT payload contract** (`schema_version: 1`) and the config/entity surface as
stable. No functional change from 0.2.38 — this is the stability milestone.

### Added
- README dashboard screenshot (rendered from the demo scene; synthetic traffic
  around the White House, no real location).

### Fixed
- Example dashboard: nearest-aircraft card rounds the distance to 0.1 nm instead
  of printing the raw float (`1.50140… nm` → `1.5 nm`).

## [0.2.38] — 2026-06-23
### Added
- `docs/demo/` — a self-contained demo scene for README screenshots: curated
  aircraft/drone fixtures positioned around the White House (no personal data), a
  stdlib `serve.py` that feeds them over HTTP with a live `now` + climbing
  `messages` (realistic msg/s), and a `config.yaml` that turns on every feature.
  Exercises country flags, military/interesting/emergency flags + alerts, the
  helicopter map icon, drone + operator + FAA make/model, and the new
  `spoof_suspect` flag. See `docs/demo/README.md`.

### Changed
- Example dashboard: added a "Suspected spoofed drones" card (the
  `spoof_suspect` flag feed) so the spoof-detection feature is visible.

## [0.2.37] — 2026-06-23
### Fixed
- Example dashboard: drone/operator map markers lingered at stale coordinates
  after a track was gone. A Map marker only disappears when its entity goes
  `unavailable`; the `Airspace Drone Operator` template sensor had no
  `availability` clause (its `operator_id or 'operator'` state kept it
  "available" forever), so the operator pin persisted indefinitely. Added an
  availability gate to the operator sensor (live nearest drone **and** a
  broadcast operator position) and tightened the aircraft/drone markers to also
  require a non-null latitude, so markers clear when the service purges a track.

## [0.2.36] — 2026-06-22
### Added
- **Remote ID spoof detection (Tier 1).** New `enable_spoof_detection` toggle
  adds a derived `spoof_suspect` flag to drones whose broadcast looks
  fabricated: a malformed/placeholder serial (`id_type: serial` not shaped like
  ANSI/CTA-2063-A, e.g. `0x00`), or the same free-text `self_id` broadcast by
  multiple distinct serials at once. Behavioral, not identity-based — RID has no
  authentication and a spoofer can replay genuine serials (which then resolve in
  the FAA registry), so identity validation can't catch a replay. Composes with
  the existing flag/alert/feed machinery like `orbiting`. Tier-2 kinematic
  signals are scoped in DESIGN.md against the future sighting store.

### Docs
- FEED.md: documented `self_id` + `self_id_seen` (synced with dump3411 v1.0.0).

## [0.2.35] — 2026-06-22
### Docs
- DESIGN.md: marked FAA UAS make/model enrichment as implemented (noting the
  live-cached vs prebuilt-SQLite deviation), and scoped the remaining drone-
  history slice — a **hybrid durable-sightings** store (one enriched row per
  sighting in the journal, deep-linking to dump3411 v1.0.0's own `/map` track
  history rather than re-storing the firehose) plus a behavioral
  `spoof_suspect` flag (tiered, since replayed real serials defeat identity
  validation). No code changes.

## [0.2.34] — 2026-06-20
### Fixed
- Drone-detected notification could describe the *wrong* drone (a previous or
  stale-retained one) with "unknown nm". Root cause: the example automation
  triggered on `sensor.airspace_drone_count` but rendered from
  `sensor.airspace_nearest_drone` — two separate entities. The count includes
  positionless drones (Basic ID before a Location message); the nearest sensor
  only carries positioned ones, so the trigger fired while the nearest sensor
  still held an earlier drone. Reworked to trigger on the nearest sensor's
  `track_id` attribute, making trigger and message body the same self-consistent
  entity and firing only once a positioned drone exists (so distance is always
  present). `self_id` (untrusted operator free-text, e.g. "Spoofing test") is now
  shown only as a last-resort label, never alongside a resolved make/model.

### Added
- Service emits a durable `drone_detected` log line once per drone (serial,
  make/model, self_id, distance, AGL, operator-located), after enrichment. It
  survives in journald / `docker logs` independent of MQTT retention, so past
  detections can be audited after the retained topics rotate.

## [0.2.33] — 2026-06-19
### Fixed
- Predictive AGL alert gate (`max_alt_agl_ft` on an inbound rule) now caps its
  altitude look-ahead at 120 s. It extrapolates `current AGL + vertical_rate *
  horizon` linearly; with the previous unbounded ETA, a routine descent
  projected a still-high, 20+ nm-out aircraft below the threshold and tripped the
  alert minutes early (descending bizjets at 3000+ ft firing a 1000 ft rule). The
  cap keeps the "fire before it dips into drone airspace" intent but only when the
  track will actually be low soon.

### Added
- Alert `info` attributes now include `vertical_rate_fpm`, so the per-rule
  `binary_sensor` shows the descent/climb rate that drove a predictive-AGL firing.

## [0.2.32] — 2026-06-18
### Added
- Drone payloads carry `self_id` — the Remote ID Self-ID message (free-text
  operator/flight description, e.g. `"Spoofing test"`). Additive optional field;
  no `schema_version` bump.

### Changed
- Example "drone detected" automation: triggers on the drone count **increasing**
  instead of `numeric_state above: 0`, which only fired on the single `0 -> 1`
  crossing and missed a second drone arriving while one was already tracked. The
  message now degrades gracefully (FAA make/model → UA type → "Drone") and shows
  `self_id` + AGL, so it is informative even when the registry can't resolve the
  serial (the source of the "Unknown drone" / blank values).

## [0.2.31] — 2026-06-18
### Changed
- Example map uses `label_mode: icon` markers: a type-aware aircraft icon
  (✈️ / 🚁 for rotorcraft), a game-controller drone, operator pin, and the alert
  icon for flagged aircraft — via small template sensors documented at the bottom.

## [0.2.30] — 2026-06-17
### Added
- Flag-feed sensors expose the nearest match's `latitude`/`longitude`, so
  `sensor.airspace_flag_<flag>` can be plotted on the HA Map (nearest military /
  interesting / … as their own markers).

### Changed
- Example dashboard rebuilt as a `sections` view (Nearest Traffic / Overview /
  Alerts) with headings and receiver/drone web-UI shortcuts; map now includes the
  nearest military + interesting markers.

## [0.2.29] — 2026-06-17
### Added
- Remote ID feeds now get Home Assistant health entities (status / drone count /
  message rate), like ADS-B receivers — they already published under
  `receiver/<name>/...` but had no discovery entities. Example "Receivers" card
  renamed to "Receivers & feeds" and includes the feed.

## [0.2.28] — 2026-06-17
### Changed
- Example dashboard: brought back the **Overview** card as an icon-grid `glance`
  (Aircraft / Drones / Nearest / flag counts) that is *also* the tap-to-popup
  surface — restores the icon layout while keeping the detail popups in one card.

## [0.2.27] — 2026-06-16
### Added
- Nearest-aircraft payload exposes `entity_picture` (the photo), so the HA Map
  marker shows the aircraft photo instead of name initials.

### Changed
- Example dashboard: consolidated the redundant "Overview" glance into the
  "tap for details" list; the standalone "Nearest aircraft" card now uses the
  rich popup layout (heading + detail bullets + photo). Documented optional
  template sensors for dynamic first-3-of-hex / RID-id map-marker labels.

## [0.2.26] — 2026-06-16
### Fixed
- Add-on changelog now shows in Home Assistant — HA reads `CHANGELOG.md` from the
  add-on directory, so `addon/CHANGELOG.md` symlinks to the root changelog
  ("No changelog found" before).
- Add-on README (its HA long-description) now uses absolute links to the project,
  docs, and example dashboard/automations; the broken relative `../README.md`
  link is fixed.

## [0.2.25] — 2026-06-16
### Added
- README "Stable interfaces" (v1 contract) + "Troubleshooting" sections.
- Add-on `icon.png` + `logo.png` artwork.

### Changed
- The add-on now displays as **"Airspace"** in Home Assistant; the slug, package,
  repo, and image names stay `ha-airspace`.

## [0.2.24] — 2026-06-15
### Added
- Inbound alerts (`max_closest_approach_nm` + `max_alt_agl_ft`) are altitude-aware:
  the AGL gate projects the track's altitude to closest-approach time using its
  vertical rate and tests the lower of now-vs-projected, so descending traffic
  dipping into drone airspace fires before it is actually low.

## [0.2.23] — 2026-06-15
### Added
- `LICENSE` (MIT) and this `CHANGELOG`.

### Fixed
- Vertical rate now falls back to `geom_rate` when a receiver reports only the
  geometric rate (no `baro_rate`); a `baro_rate` of `0` (level flight) is still
  preserved rather than masked.

### Changed
- Privacy: maintainer location, LAN IP, and ground elevation scrubbed from
  examples / tests / fixtures; the example watchpoint is now the White House.

## [0.2.22] — 2026-06-15
### Fixed
- Backfill `aircraft_type` from the reference DB (`db_metadata.type`) when the
  broadcast omits `t` (military especially), so the type shows everywhere and
  `types`-based flags match off the DB.

## [0.2.21] — 2026-06-15
### Added
- Country of registration derived from the ICAO hex: `country` (ISO 3166-1
  alpha-2) + `country_flag` (emoji) on aircraft payloads, flag-feed rows, and
  alert info. New `icao_country` module.

## [0.2.19] – [0.2.20] — 2026-06-15
### Added
- `db_metadata` on flag-feed rows + example "Notes" column surfacing *why* a
  track is flagged (PIA / LADD / mil / operator).
- Planespotters photo on the standalone nearest-aircraft card.

## [0.2.18] — 2026-06-15
### Added
- Drone-conflict / approaching-aircraft alert recipe (predictive
  `max_closest_approach_nm` + `max_alt_agl_ft`) and example push automation.
- Alert info entity enriched with altitude, ETA, bearing, squawk.
- Flight/hex rendered as clickable links to globe.adsbexchange.com.

## [0.2.15] – [0.2.16] — 2026-06-14
### Added
- Planespotters photo on the nearest-aircraft summary and the nearest match of
  each flag feed (bounded, cached, fails-soft).

## [0.2.11] – [0.2.14] — 2026-06-14
### Added
- Per-flag feed sensors (`sensor.airspace_flag_<flag>`) with a distance-sorted
  list of matching aircraft.
- `enable_drone_registry` add-on toggle; documented `extra_config` merge behavior.
- Example HA automations and a `browser_mod` "tap for details" dashboard card.

## [0.2.10] — 2026-06-14
### Added
- FAA UAS make/model enrichment for drones by broadcast serial (`drone_registry`).

## [0.2.8] — 2026-06-14
### Changed
- Add-on ships **prebuilt per-arch images** (aarch64, amd64); the Supervisor
  pulls instead of building on-device for fast, reliable updates.

## [0.1.0] – [0.2.9] — 2026-06-13 … 06-14
Initial development. Established the full pipeline and contracts:
### Added
- Receivers: `HttpJsonReceiver` (dump1090-fa / readsb / dump1090-mutability /
  dump978) + Remote ID feed, with auth and receiver-location auto-detect.
- Multi-source merger, enrichment (declarative flags + ENTER/EXIT alerts),
  SQLite journal (durable `first_seen`, history-aware alerts), predictive
  closest-approach, orbit/loiter detection, Planespotters photos.
- MQTT publisher + Home Assistant MQTT discovery; base topic `airspace`.
- Home Assistant add-on with batteries-included toggles; Docker image;
  optional Prometheus `/metrics`.
- Pydantic v2 config (strict, fail-fast), structlog logging.

[Unreleased]: https://github.com/ifnull/ha-airspace/compare/v1.1.0...HEAD
