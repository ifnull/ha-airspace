# Changelog

All notable changes to `ha-airspace` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[Semantic Versioning](https://semver.org/).

The **published MQTT payload contract** is versioned separately via
`schema_version` (see [README → Stable interfaces](README.md#stable-interfaces)).
Additive, optional payload fields are backwards-compatible and do **not** bump
the major version; a removed/renamed/retyped field does.

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

[Unreleased]: https://github.com/ifnull/ha-airspace/compare/v0.2.26...HEAD
