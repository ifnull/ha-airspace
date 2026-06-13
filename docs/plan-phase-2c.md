# Phase 2c implementation plan — Planespotters photo enrichment

**Goal (DESIGN "Phase 2c: Delight", done-when):** alert payloads carry a photo
URL when configured. Off by default, cached, fails-soft, **alert payloads only**
— never the high-cardinality wildcard `adsb/aircraft/<hex>` topic. The
predictive schema fields (the other half of 2c) already ship as `None`
placeholders, so this slice is just photos.

## Source

`GET https://api.planespotters.net/pub/photos/hex/{hex}` → `{"photos": [{
"thumbnail": {"src", "size"}, "thumbnail_large": {"src"}, "link",
"photographer" }]}`. No API key. `{"photos": []}` when none. We take the first
photo's thumbnail + its attribution (link + photographer — Planespotters asks
for attribution, so we publish it alongside the URL).

## Design

- **`PhotosConfig`** (config.py): `enabled: bool = False`,
  `cache_ttl_days: float = 30`, `inject_into: list[Literal["alerts"]] =
  ["alerts"]`. Added to `Config` (default-factory). Strict-validated.
- **`PhotoEnricher`** (new `photos.py`): async `photo_for(hex) -> PhotoPayload |
  None`. In-memory TTL cache (hex → result + monotonic stamp); caches misses too
  so a photoless hex isn't refetched every alert. **Fails-soft** — any
  timeout/HTTP/parse error logs a warning and returns `None`; an alert never
  blocks on or breaks from a photo lookup. Takes an injected `httpx.AsyncClient`
  (tests use `MockTransport`; **no network in tests**). In-memory only — no disk
  writes (SD-card-friendly).
- **Payloads** (payloads.py): `PhotoPayload{thumbnail_url, link, photographer}`
  and `AlertPayload(AircraftPayload)` adding `photo: PhotoPayload | None`. The
  alert topic reuses the full aircraft contract + photo; the wildcard keeps
  plain `AircraftPayload` (no photo field at all), honoring "alerts only".
  `AlertPayload.build(state, photo)` constructs it.
- **Wiring**:
  - `publisher.publish_alert(rule, state, photo=None)` builds `AlertPayload`
    instead of `AircraftPayload`.
  - `tracker._evaluate_alerts`: on ENTER, if a photo enricher is present, fetch
    the photo for the canonical hex (short timeout, fails-soft) and pass it to
    `publish_alert`. EXIT path unchanged.
  - `app.build_app`: when `photos.enabled`, build an `httpx.AsyncClient`
    (descriptive User-Agent, ~5s timeout) + `PhotoEnricher`, inject into the
    tracker, and close the client on shutdown.
- **Docs**: add the `photos:` block to `config.example.yaml`.

## Out of scope

- No new HA entity / discovery change — the photo rides in the alert JSON;
  consumers read it via `value_template`. (Matches DESIGN.)
- No journaling/persistence of photos. No predictive implementation (Phase 5).
- Only `inject_into: ["alerts"]` is valid for now (schema is forward-looking).

## Testing

- `test_photos.py`: hit → `PhotoPayload`; empty → `None`; HTTP 500 / timeout →
  `None` (fails-soft); cache hit avoids a second fetch; TTL expiry refetches
  (injected clock). All via `httpx.MockTransport` — no network.
- payloads: `AlertPayload.build` carries the photo; `AircraftPayload` has no
  `photo` field (wildcard stays clean).
- tracker: alert ENTER with an enricher passes the photo through to
  `publish_alert`; with no enricher, alerts still publish (photo `None`).
- config: `PhotosConfig` defaults + rejects unknown `inject_into` targets.
