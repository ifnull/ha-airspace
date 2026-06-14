# ha-airspace — Home Assistant add-on

Runs the [`ha-airspace`](../README.md) ADS-B + drone Remote ID enrichment
service as a Home Assistant add-on: receivers in, enriched aircraft/drone
entities out over MQTT, no custom integration required.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories**, add:
   `https://github.com/ifnull/ha-airspace`
2. Install **ha-airspace** from the store.
3. Configure receivers + watchpoints (and MQTT, unless you use the Mosquitto
   add-on — then leave MQTT blank), then start.

Full option reference: [DOCS.md](DOCS.md).

## Architecture

The add-on image is a thin layer over the published multi-arch service image
(`ghcr.io/ifnull/ha-airspace`): `run.sh` calls `render_config.py` to translate
the Supervisor's add-on options into the service's native `config.yaml`, then
`exec`s the service so it receives SIGTERM directly for graceful shutdown.

The option→config translation is unit-tested against the real config schema in
`tests/test_addon_render.py`, so the add-on and the service can't silently drift.

The add-on ships as **prebuilt per-arch images** (`config.yaml` `image:` →
`ghcr.io/ifnull/{arch}-addon-ha-airspace`), so the Supervisor *pulls* it rather
than building on the device — fast, reliable installs and updates. The release
workflow builds those images (FROM the service image) on each `v*` tag.
`build.yaml` is kept for local development builds.

## Still TODO before store release

- `icon.png` / `logo.png` add-on artwork.
- Make the `*-addon-ha-airspace` GHCR packages **public** (one-time, like the
  service image) so the Supervisor can pull them without auth.
