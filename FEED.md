# Remote ID feed contract (`remoteid.json`)

**Status:** draft / Phase 3 target. Canonical source of truth for the JSON feed
that a Remote ID detector serves over HTTP and that `ha-airspace` consumes as a
source. Both `ha-airspace` and the producer (e.g. `drone-aware-zero`) build to
this document; neither imports the other.

> This is **not** `aircraft.json` and is **not** intended for dump1090-family
> tools. Drones are not manned aircraft — the schema is purpose-built for ASTM
> F3411 Remote ID and carries data (operator location, UAS ID type, native AGL)
> that has no place in the dump1090 schema. We deliberately reuse dump1090's
> *envelope idioms* (`now`, `seen`, a polled array) because `ha-airspace`
> already handles them — not the aircraft *schema*.

---

## Transport

- **Method:** `GET`
- **Path:** `/data/remoteid.json` (mirrors dump1090's `/data/aircraft.json`
  location convention; the filename makes the content type unambiguous).
- **Content-Type:** `application/json`
- **Cadence:** the producer refreshes the document at ~1 Hz; the consumer polls
  on its own interval (default 1 Hz). The feed is a *snapshot of current
  detections*, not an event log.
- **Auth:** none assumed on a trusted LAN. If the producer is exposed, the
  consumer supports the same HTTP auth options as any other receiver
  (`ha-airspace` receiver config: bearer/basic). Out of scope for the producer
  to mandate.

The producer must be cheap to serve: serve a pre-built in-memory snapshot, do
**no** decoding or work per request. (On a Pi Zero W the 802.11/BLE decode
already saturates the single core — see "Producer requirements".)

---

## Envelope

```jsonc
{
  "schema_version": 1,          // bump on breaking changes; consumer rejects unknown major
  "now": 1717200000.0,          // epoch seconds, producer wall clock at snapshot time
  "messages": 4213,             // total RID messages decoded since producer start (monotonic)
  "drones": [ /* Detection objects, see below */ ]
}
```

- `schema_version` — integer. Consumers ignore feeds whose major version they
  don't understand rather than mis-parsing.
- `now` — the reference clock for every `seen`/`seen_pos`/`self_id_seen`/
  `operator.seen` in the document.
  Consumers convert to absolute timestamps at ingest using *their own* receipt
  time anchored to `now` (do **not** trust the producer's absolute clock for
  ordering; only the relative `seen` deltas).
- `messages` — health/liveness signal. A stalled counter across polls means the
  detector is up but hearing nothing (or wedged); the consumer surfaces this.
- `drones` — array, possibly empty. Empty is normal and expected (nothing
  compliant flying nearby).

---

## Detection object

| Field | Type | Unit | Req | Notes |
|---|---|---|:--:|---|
| `id` | string | — | ✓ | The UAS ID (`uas_id`). The identity key. ASCII, ≤20 chars. |
| `id_type` | string | — | ✓ | `serial` \| `caa_reg` \| `utm_uuid` \| `session` \| `unknown`. |
| `ua_type` | string | — |  | UA category from Basic ID, e.g. `helicopter`, `aeroplane`, `rotorcraft`. Omit if unknown. |
| `lat` | number | deg |  | WGS-84. Omit the whole pair if no positional fix yet. |
| `lon` | number | deg |  | WGS-84. |
| `alt_geom_ft` | number | ft |  | Geometric (WGS-84) altitude. RID has no barometric alt. |
| `agl_ft` | number | ft |  | Height above takeoff/ground, broadcast natively by RID. |
| `gs` | number | kt |  | Ground speed. |
| `track` | number | deg |  | True track, 0–360. |
| `geom_rate` | number | ft/min |  | Geometric vertical rate (RID broadcasts geometric, not baro). |
| `rssi` | number | dBm |  | Receive signal strength of the last decoded message. |
| `message_count` | number | — |  | Running count of decoded RID messages for this `id` since first seen (per-drone; distinct from the envelope-level `messages`). |
| `seen` | number | s | ✓ | Seconds since the last message from this `id`, relative to `now`. |
| `seen_pos` | number | s |  | Seconds since the last *positional* message. Omit if never positioned. |
| `rid_source` | string | — |  | `ble` \| `wifi_beacon` \| `wifi_nan`. Transport the detection arrived on. |
| `self_id` | string | — |  | Free-text description from the Self-ID message (operator-set flight purpose/intent, e.g. `"inspection"`). **Untrusted, operator-controlled** — never an identity. Omit if no Self-ID message seen. |
| `self_id_seen` | number | s |  | Seconds since the last Self-ID message, relative to `now` — staleness of `self_id`. Omit if never seen. |
| `operator` | object | — |  | Present only once a System/Operator-ID message has been seen for this `id`. See below. |

### `operator` sub-object

| Field | Type | Unit | Req | Notes |
|---|---|---|:--:|---|
| `lat` | number | deg |  | Operator (pilot/ground-station) latitude. The novel, security-relevant field. |
| `lon` | number | deg |  | Operator longitude. |
| `location_type` | string | — |  | `takeoff` \| `live_gnss` \| `fixed` — what `lat`/`lon` actually represent (System msg byte 1, bits 0-1). **`takeoff` is the drone's own launch point, not a live operator position.** Some transmitters toggle this across messages, so a consumer that always treats `lat`/`lon` as "where the operator is standing right now" will see it jump between the drone's launch point and the operator's actual position. Omit if the producer doesn't decode it — a consumer MUST NOT assume omitted means `live_gnss`; treat unknown as unknown. |
| `id` | string | — |  | Operator ID (often a CAA operator registration). |
| `alt_takeoff_ft` | number | ft |  | Takeoff altitude, geometric. |
| `seen` | number | s |  | Seconds since the last System/Operator-ID message, relative to `now` — staleness of this whole block. |

A detection with **no** `lat`/`lon` is valid — RID transmitters broadcast Basic
ID before GPS lock, and a drone may be heard before its Location message. The
consumer keeps the track (identity is known) and marks position unavailable.

---

## Semantics the consumer relies on

- **Identity:** the track is keyed on `id` (a string), **not** an ICAO hex.
  Inside `ha-airspace` a Remote ID track is `band: "remoteid"`; the identity
  model is source-agnostic, so `id` is stored as the track key directly without
  pretending to be a 24-bit ICAO address. No reference-DB (Mictronics/
  adsbexchange) lookup is attempted for `remoteid` tracks.
- **Staleness / removal:** the producer drops an `id` from `drones` once it has
  not been heard for the producer's own timeout. The consumer additionally ages
  out any track whose `seen` exceeds its configured max age and purges the
  retained MQTT topic (empty-retained), exactly as it does for ADS-B.
- **Units:** all distances/speeds/altitudes are pre-converted by the producer to
  `ha-airspace`'s canonical units (**ft, kt, ft/min**), even though RID
  broadcasts SI (m, m/s). This keeps a single conversion path on the consumer
  side. The spec is unit-explicit precisely so the producer owns the conversion.
- **Dedup across bands:** if the same physical airframe were ever heard on both
  a `remoteid` feed and (hypothetically) an ADS-B feed, the merger dedups by
  identity per band — Remote ID `id` and ICAO hex are different namespaces and
  are **not** cross-matched. A drone is its own track.
- **Multi-transport precedence:** a producer that hears the same `id` on more
  than one transport (BLE, WiFi beacon, WiFi NAN) collapses them into one
  detection — the latest message wins for `rid_source`, `rssi`, and every
  time-varying field (position, velocity, heading, altitude, `self_id`, operator
  block). Identity fields (`id_type`, `ua_type`) are write-once from the first
  Basic ID.
  `message_count` increments on every decoded message regardless of transport.
  The consumer therefore sees a single track per `id` with `rid_source`
  reflecting the most recent hop.

---

## Example

```json
{
  "schema_version": 1,
  "now": 1717200000.0,
  "messages": 4213,
  "drones": [
    {
      "id": "1581F5EXAMPLE0001",
      "id_type": "serial",
      "ua_type": "helicopter",
      "lat": 40.7128,
      "lon": -74.0060,
      "alt_geom_ft": 412,
      "agl_ft": 380,
      "gs": 12.3,
      "track": 271.0,
      "geom_rate": -64,
      "rssi": -62.0,
      "message_count": 137,
      "seen": 0.4,
      "seen_pos": 0.6,
      "rid_source": "ble",
      "self_id": "inspection",
      "self_id_seen": 1.8,
      "operator": {
        "lat": 40.6900,
        "lon": -74.0100,
        "location_type": "live_gnss",
        "id": "FA3OPERATOR123",
        "alt_takeoff_ft": 50,
        "seen": 2.3
      }
    },
    {
      "id": "OP-SESSION-77F2",
      "id_type": "session",
      "message_count": 4,
      "seen": 1.1,
      "rid_source": "wifi_beacon"
    }
  ]
}
```

---

## Producer requirements

- **Single in-memory cache, keyed by `id`.** One process, one dict, one lock.
  Where a producer has multiple radios (e.g. `drone-aware-zero`'s BLE + WiFi),
  they run as threads inside that process and write into the same cache rather
  than serving separate feeds. Each entry holds the latest decoded fields plus
  per-id monotonic timestamps `last_seen`, `last_pos_seen`, and
  `last_operator_seen`.
- **Snapshot, not per-request work.** The HTTP handler copies the live state
  under the lock, releases the lock, then serializes — nothing else. On a Pi
  Zero W the BLE/802.11 decode is the budget; the HTTP path must be ~free.
- **Timestamps computed at serialize time:** `seen = now - last_seen[id]`,
  likewise `seen_pos`, `self_id_seen`, and `operator.seen` from their respective
  timestamps.
- **Drop stale ids** from the cache on the producer's own timeout (default 60 s
  with no messages on any transport) so the feed reflects current airspace.
- **Additive.** Serving this feed must not change existing detector behavior
  (e.g. journald logging). It's an opt-in mode (`--serve <addr:port>`).

## Consumer requirements (`ha-airspace`)

- A dedicated `RemoteIdHttpReceiver` (or `HttpJsonReceiver` with a `remoteid`
  mapping profile) reusing the shared HTTP polling/backoff/`health()` plumbing.
- Maps the Detection object → the internal source-agnostic track model with
  `band="remoteid"`.
- Publishes its own HA discovery surface (drone count, nearest drone, operator
  location) distinct from the aircraft entities — drones are not aircraft in HA
  either.

---

## Versioning

`schema_version` is a single integer. Additive, backward-compatible changes
(new optional fields) **do not** bump it — consumers must ignore unknown fields.
Removing or repurposing a field, or changing units/semantics, bumps the major
and is a coordinated change across both repos. Document every bump here.

| Version | Date | Change |
|---|---|---|
| 1 | (draft) | Initial contract. |
| 1 | 2026-06 | Added optional `self_id` + `self_id_seen` (Self-ID message). Additive — no major bump; consumers ignore if absent. Matches dump3411 v1.0.0. |
