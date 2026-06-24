# Demo scene (for README screenshots)

A self-contained, **no-personal-data** airspace built from curated fixtures, so
every ha-airspace feature lights up for a screenshot. The watchpoint is the
**White House** (38.8977, -77.0365) and all aircraft/drones are synthetic and
positioned as offsets from it.

What the scene exercises:

| Feature | How it shows up |
|---|---|
| Nearest aircraft + photo | `UAL245` ~1.5 nm NE (Planespotters photo by hex) |
| Country-of-registration flags | 🇺🇸 US, 🇩🇪 GAF123, 🇬🇧 RRR2145 |
| Military flag + alert | RCH804 (C17), EVAC11 (🚁), GAF123, RRR2145 |
| Interesting flag | N700XJ (Global Express) |
| Emergency flag + alert | DAL9, squawk 7700 |
| Helicopter map icon | EVAC11 (category A7 → 🚁) |
| Drone + operator + FAA make/model | DJI Mavic 4 Pro, operator pin, AGL |
| **Spoof detection** (`spoof_suspect`) | `0x00` (malformed serial) + two drones sharing self_id "Spoofing test" |
| Two receivers + RID feed health | rx-1090, rx-978, dump3411 with live msg/s |

## Run it

Prereqs: a running **MQTT broker + Home Assistant** (the demo publishes real
discovery entities). The demo overwrites the live `airspace/*` entities while it
runs, so stop your production service first (or point the demo at a throwaway HA).

```bash
# 1) make your local config (config.yaml is git-ignored, so creds stay out of git)
cp docs/demo/config.example.yaml docs/demo/config.yaml
#    edit docs/demo/config.yaml -> set mqtt.broker / username / password

# 2) serve the fixtures (live `now` + climbing `messages` so msg/s is realistic)
cd docs/demo && python3 serve.py            # http://127.0.0.1:8765

# 3) run the service against the demo config (another shell)
ha-airspace --config docs/demo/config.yaml
# or: uv run ha-airspace --config docs/demo/config.yaml
```

> `docs/demo/config.yaml` is git-ignored (the repo ignores every `config.yaml`),
> so your broker credentials never get committed. The committed template is
> `config.example.yaml`.

In Home Assistant:

1. Add the **map template sensors** from the bottom of
   [`../dashboard.example.yaml`](../dashboard.example.yaml) to your
   `configuration.yaml`, then **reload template entities** (or restart). Without
   them the map markers won't render.

   **Swap `zone.home` on the map** — the example map lists `zone.home` as the
   observer marker, which is *your real house*. For screenshots, replace it with a
   fixed White-House watchpoint pin so nothing personal leaks:

   ```yaml
   # add alongside the other airspace_*_map template sensors:
   - name: Airspace Watchpoint
     unique_id: airspace_watchpoint
     state: home
     icon: mdi:home-map-marker
     attributes:
       latitude: 38.8977
       longitude: -77.0365
   ```
   ```yaml
   # in the map card, change the entities entry:
   - entity: sensor.airspace_watchpoint   # was: zone.home
     label_mode: icon
   ```
2. Add a dashboard from `../dashboard.example.yaml`.
3. The drone-conflict / spoof cards need `enable_spoof_detection` (already on in
   the demo config).

Give it ~5 seconds after start so the per-track throttles settle and the
duplicate-`self_id` spoof flag converges across both drones.

## Two public lookups

Per the demo config, `photos` and `drone_registry` are **on** — they each make a
one-time public lookup (Planespotters by hex; FAA UAS registry by serial). No
personal data. If you want a fully offline run, set both to `enabled: false`; the
photo card and drone make/model just render in their fallback state.

**Photo not showing?** The nearest aircraft `a3f8c1` (UAL245) is the photo
target — Planespotters needs a real, photographed hex. If it's blank, edit
`aircraft.json` and set that aircraft's `hex` to any real, currently-photographed
aircraft (look one up on planespotters.net), then it'll resolve.

## Screenshots

Capture **both**:

- **One hero** — the full `sections` dashboard, for the top of the README.
- **Per-feature crops** — the map, the nearest-aircraft card (with photo), the
  Overview glance, Receivers & feeds, the Emergency/Military/Interesting tables,
  and the Suspected-spoofed-drones card. The full board is illegible at README
  width; the crops are where readers actually see each feature.

## Cleanup

Stop `serve.py` and the demo service, then restart your production service with
your real config. The demo's retained topics clear on the next real publish (or
clear `airspace/#` retained if you switch HA instances).
