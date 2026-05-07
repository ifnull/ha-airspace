# adsb-enrich

A multi-source ADS-B enrichment service that consumes `aircraft.json` from one or more dump1090 / readsb / dump978-fa receivers, joins against reference databases, applies tagging/alert rules, and publishes to MQTT for Home Assistant.

**Status:** Phase 1 in progress. Not yet shippable.

See [`DESIGN.md`](DESIGN.md) for architecture and roadmap. See [`CLAUDE.md`](CLAUDE.md) for development conventions.
