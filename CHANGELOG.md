# Changelog

All notable changes to `ha-airspace` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[Semantic Versioning](https://semver.org/).

The **published MQTT payload contract** is versioned separately via
`schema_version` (see [README → Stable interfaces](README.md#stable-interfaces)).
Additive, optional payload fields are backwards-compatible and do **not** bump
the major version; a removed/renamed/retyped field does.

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

[Unreleased]: https://github.com/ifnull/ha-airspace/compare/v0.2.33...HEAD
