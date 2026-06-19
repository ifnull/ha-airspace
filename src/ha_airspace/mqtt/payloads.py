"""Pydantic models for the JSON payloads published to MQTT.

These ARE the external API surface — what HA, Grafana, Node-RED, custom
scripts, and every other consumer sees. They live separately from
``ha_airspace.models`` (internal runtime types) so the boundary between
"private state" and "public contract" is visible at the import path.

Versioning posture: the shape evolved freely through Phases 1-3 (Daniel
the only consumer). Phase 4 freezes it: every consumer-facing entity
payload now carries ``schema_version`` (``PAYLOAD_SCHEMA_VERSION``) as its
first field, so a downstream consumer can branch on the contract version
before reading anything else. Bump the constant on any breaking change to
the published field set. Receiver stats/location payloads stay unversioned
— they are internal diagnostics, not the public entity contract.

Phase-2 fields (``flags``, ``db_metadata``, ``predicted_*``) are
present in Phase 1 payloads but always empty / None. The shape stays
stable; Phase 2 just fills them in.

Serialization: callers invoke ``model_dump_json()`` to produce bytes
ready for MQTT publish. Datetimes serialize to ISO 8601 with explicit
offset (e.g. ``2026-01-01T12:00:00+00:00``); both HA and standard JSON
parsers handle this. Sets serialize as sorted lists for deterministic
output.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from ha_airspace.icao_country import country_for, flag_for
from ha_airspace.models import AircraftState, ReceiverLocation

PAYLOAD_SCHEMA_VERSION = 1
"""Version of the published MQTT entity-payload contract. Frozen at Phase 4.
Bump on any breaking change to the ``AircraftPayload`` / ``DronePayload`` field
set (removed/renamed/retyped field); additive optional fields do not require a
bump. Emitted as ``schema_version`` on every consumer-facing entity payload."""


class PhotoPayload(BaseModel):
    """An aircraft photo (Planespotters, Phase 2c). ``link`` + ``photographer``
    are the attribution Planespotters asks consumers to display alongside the
    image. Populated on alert payloads and the nearest-aircraft summary; never
    the high-cardinality ``airspace/aircraft/<hex>`` wildcard (no lookup there)."""

    model_config = ConfigDict(extra="forbid")

    thumbnail_url: str
    link: str | None = None
    photographer: str | None = None


class AircraftPayload(BaseModel):
    """Serialized form of an ``AircraftState`` for the
    ``airspace/aircraft/<hex>`` topic.

    Power-user wildcard topic only — never auto-discovered as HA
    entities (would explode HA's entity registry). Subscribed to
    directly by Grafana, Node-RED, custom scripts.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Contract ------------------------------------------------------
    schema_version: int = PAYLOAD_SCHEMA_VERSION
    """Published payload contract version. See ``PAYLOAD_SCHEMA_VERSION``."""

    # --- Identity ------------------------------------------------------
    track_id: str
    """The merge key: ICAO hex for ADS-B, UAS id for Remote ID. Always present
    — the stable per-track identifier consumers should key on."""
    hex: str | None = None
    """ICAO hex, or ``None`` for non-ICAO (Remote ID) tracks."""
    flight: str | None = None
    registration: str | None = None
    squawk: str | None = None

    # --- Position ------------------------------------------------------
    lat: float | None = None
    lon: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    """HA-map aliases of ``lat``/``lon``: the Map card plots any entity exposing
    ``latitude``/``longitude`` attributes, so the ``nearest`` sensor lands on the
    map natively. Same values as ``lat``/``lon`` (kept for existing consumers)."""
    alt_baro_ft: int | None = None
    alt_geom_ft: int | None = None

    # --- Movement ------------------------------------------------------
    ground_speed_kt: float | None = None
    track_deg: float | None = None
    vertical_rate_fpm: int | None = None
    on_ground: bool | None = None

    # --- Provenance / classification ----------------------------------
    bands: list[str]
    """Sorted list of bands the canonical observation came in on
    (Phase 1 always single-element; Phase 3 may be 2 for 1090+978)."""
    seen_by: list[str]
    """Sorted list of receiver names that have ever observed this
    hex. Phase 1 single-element; Phase 3+ may be more."""
    category: str | None = None
    aircraft_type: str | None = None
    is_tisb: bool = False
    country: str | None = None
    """ISO 3166-1 alpha-2 country of registration, derived from the ICAO hex."""
    country_flag: str | None = None
    """Flag emoji for ``country`` (ready to print), or ``None`` for non-ICAO/unallocated."""

    # --- Timing --------------------------------------------------------
    first_seen: datetime
    last_seen: datetime

    # --- Geometry (per-watchpoint) ------------------------------------
    distance_to: dict[str, float]
    """Watchpoint name -> great-circle distance in nautical miles."""
    bearing_to: dict[str, float]
    """Watchpoint name -> bearing in degrees (0=N, 90=E)."""

    # --- Predictive (schema reserved Phase 2c, impl Phase 5) -----------
    predicted_eta_to_home_s: float | None = None
    predicted_closest_approach_nm: float | None = None

    # --- Enrichment (Phase 2a+) ---------------------------------------
    flags: list[str]
    """Sorted list of flag names from rule evaluation. Empty in Phase 1."""
    db_metadata: dict[str, Any]
    """Mictronics + ADSBex merged fields. Empty dict in Phase 1."""

    # --- Photo (Phase 2c) ---------------------------------------------
    photo: PhotoPayload | None = None
    """Planespotters photo. Populated on alert payloads and the nearest-aircraft
    summary (when photos are enabled and a photo exists); ``None`` on the
    high-cardinality ``airspace/aircraft/<hex>`` wildcard — no lookup is done
    there, preserving the per-hex cost guarantee."""
    entity_picture: str | None = None
    """The photo thumbnail URL, flattened to the attribute name Home Assistant's
    Map card / more-info use for an entity image — so the nearest-aircraft marker
    renders the photo instead of name initials. Mirrors ``photo.thumbnail_url``;
    ``None`` when there's no photo."""

    @classmethod
    def from_state(cls, state: AircraftState, photo: PhotoPayload | None = None) -> Self:
        """Project an internal ``AircraftState`` into the published
        payload shape. Sets are sorted for deterministic JSON output
        (some MQTT consumers cache by message hash; non-deterministic
        ordering causes false cache misses).

        ``photo`` is attached for the nearest-aircraft summary and alert payloads;
        callers for the wildcard pass nothing, leaving it ``None``.
        """
        canonical = state.canonical
        return cls(
            track_id=state.track_id,
            hex=state.hex,
            flight=canonical.flight,
            registration=canonical.registration,
            squawk=canonical.squawk,
            lat=canonical.lat,
            lon=canonical.lon,
            latitude=canonical.lat,
            longitude=canonical.lon,
            alt_baro_ft=canonical.alt_baro_ft,
            alt_geom_ft=canonical.alt_geom_ft,
            ground_speed_kt=canonical.ground_speed_kt,
            track_deg=canonical.track_deg,
            vertical_rate_fpm=canonical.vertical_rate_fpm,
            on_ground=canonical.on_ground,
            bands=sorted(state.bands),
            seen_by=sorted(state.seen_by),
            category=canonical.category,
            aircraft_type=canonical.aircraft_type,
            is_tisb=canonical.is_tisb,
            country=country_for(state.hex),
            country_flag=flag_for(state.hex),
            first_seen=state.first_seen,
            last_seen=state.last_seen,
            distance_to=dict(state.distance_to),
            bearing_to=dict(state.bearing_to),
            predicted_eta_to_home_s=state.predicted_eta_to_home_s,
            predicted_closest_approach_nm=state.predicted_closest_approach_nm,
            flags=sorted(state.flags),
            db_metadata=dict(state.db_metadata),
            photo=photo,
            entity_picture=photo.thumbnail_url if photo else None,
        )


class DronePayload(BaseModel):
    """Serialized form of a Remote ID (drone) ``AircraftState`` for the
    ``airspace/drone/<track_id>`` topic and the ``airspace/summary/nearest_drone``
    sensor.

    Drones are not aircraft: this carries the Remote-ID-only fields (UAS id
    type, native AGL, transport, and — the security-relevant part — operator
    location) that have no place on ``AircraftPayload``. Distance/bearing are
    the drone's own; operator distance/bearing are computed by the consumer
    from ``operator_lat``/``operator_lon`` if wanted.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Contract ------------------------------------------------------
    schema_version: int = PAYLOAD_SCHEMA_VERSION
    """Published payload contract version. See ``PAYLOAD_SCHEMA_VERSION``."""

    # --- Identity ------------------------------------------------------
    track_id: str
    """UAS id — the stable per-drone key."""
    id_type: str
    """``serial`` | ``caa_reg`` | ``utm_uuid`` | ``session`` | ``unknown``."""
    ua_type: str | None = None
    self_id: str | None = None
    """Free-text operator/flight description from the Self-ID message."""

    # --- Position / movement ------------------------------------------
    lat: float | None = None
    lon: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    """HA-map aliases of ``lat``/``lon`` (the drone's own position), so the
    nearest-drone sensor plots on the Map card natively."""
    alt_geom_ft: int | None = None
    agl_ft: float | None = None
    """Height above takeoff/ground, broadcast natively by Remote ID."""
    ground_speed_kt: float | None = None
    track_deg: float | None = None
    vertical_rate_fpm: int | None = None

    # --- Operator (the novel, security-relevant entity) ---------------
    operator_lat: float | None = None
    operator_lon: float | None = None
    operator_id: str | None = None
    operator_alt_takeoff_ft: float | None = None

    # --- Provenance ----------------------------------------------------
    rid_source: str | None = None
    seen_by: list[str]
    first_seen: datetime
    last_seen: datetime

    # --- Geometry (drone position, per-watchpoint) --------------------
    distance_to: dict[str, float]
    bearing_to: dict[str, float]

    # --- Enrichment ----------------------------------------------------
    flags: list[str]
    db_metadata: dict[str, Any]
    """FAA UAS registry fields (make/model/status) when drone_registry is on;
    empty otherwise. Compliance/product data, not operator identity."""

    @classmethod
    def from_state(cls, state: AircraftState) -> Self:
        """Project a ``band="remoteid"`` ``AircraftState`` into the drone
        payload. ``state.canonical.drone`` carries the RID-only fields."""
        canonical = state.canonical
        drone = canonical.drone
        return cls(
            track_id=state.track_id,
            id_type=drone.id_type if drone else "unknown",
            ua_type=drone.ua_type if drone else None,
            self_id=drone.self_id if drone else None,
            lat=canonical.lat,
            lon=canonical.lon,
            latitude=canonical.lat,
            longitude=canonical.lon,
            alt_geom_ft=canonical.alt_geom_ft,
            agl_ft=drone.agl_ft if drone else None,
            ground_speed_kt=canonical.ground_speed_kt,
            track_deg=canonical.track_deg,
            vertical_rate_fpm=canonical.vertical_rate_fpm,
            operator_lat=drone.operator_lat if drone else None,
            operator_lon=drone.operator_lon if drone else None,
            operator_id=drone.operator_id if drone else None,
            operator_alt_takeoff_ft=drone.operator_alt_takeoff_ft if drone else None,
            rid_source=drone.rid_source if drone else None,
            seen_by=sorted(state.seen_by),
            first_seen=state.first_seen,
            last_seen=state.last_seen,
            distance_to=dict(state.distance_to),
            bearing_to=dict(state.bearing_to),
            flags=sorted(state.flags),
            db_metadata=dict(state.db_metadata),
        )


class FlagAircraft(BaseModel):
    """One compact, display-oriented row in a flag feed (``FlagFeedPayload``).

    A flattened subset of ``AircraftPayload`` — just the fields a glance/table
    card shows for a flagged aircraft (altitude, distance, type, squawk, flags).
    ``distance_nm`` / ``bearing_deg`` are relative to the feed's watchpoint, so
    the card needs no nested-dict templating to render a row.
    """

    model_config = ConfigDict(extra="forbid")

    track_id: str
    hex: str | None = None
    flight: str | None = None
    registration: str | None = None
    aircraft_type: str | None = None
    alt_baro_ft: int | None = None
    squawk: str | None = None
    country_flag: str | None = None
    """Flag emoji for the country of registration (from the ICAO hex), or None."""
    distance_nm: float | None = None
    """Great-circle distance to the feed's watchpoint, nm. None if unpositioned."""
    bearing_deg: float | None = None
    """Bearing from the feed's watchpoint toward the aircraft, degrees."""
    flags: list[str]
    db_metadata: dict[str, Any]
    """DB-derived fields (registration, operator, ``pia``/``ladd``/``mil`` markers,
    …) so a flag table can show *why* a track is flagged — e.g. PIA vs LADD for an
    ``interesting`` row. Empty dict when no databases are configured."""

    @classmethod
    def from_state(cls, state: AircraftState, *, watchpoint: str) -> Self:
        """Project a state into a feed row, with distance/bearing taken from the
        given watchpoint (the feed's primary)."""
        c = state.canonical
        return cls(
            track_id=state.track_id,
            hex=state.hex,
            flight=c.flight,
            registration=c.registration,
            aircraft_type=c.aircraft_type,
            alt_baro_ft=c.alt_baro_ft,
            squawk=c.squawk,
            country_flag=flag_for(state.hex),
            distance_nm=state.distance_to.get(watchpoint),
            bearing_deg=state.bearing_to.get(watchpoint),
            flags=sorted(state.flags),
            db_metadata=dict(state.db_metadata),
        )


class FlagFeedPayload(BaseModel):
    """The ``airspace/summary/by_flag/<flag>`` payload: a bounded, distance-sorted
    list of the aircraft currently carrying one flag.

    Backs one discovered ``sensor.airspace_flag_<flag>`` per configured flag
    (state = ``count``, attributes = this payload), so a card can list e.g. the
    military / interesting / ladd aircraft with detail — without per-aircraft
    entities. ``aircraft`` is capped (nearest first); ``count`` is the true
    total so the card can show "showing N of M".
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = PAYLOAD_SCHEMA_VERSION
    flag: str
    count: int
    """Total aircraft carrying the flag (may exceed ``len(aircraft)`` when capped)."""
    watchpoint: str
    """Watchpoint the rows' distance/bearing are relative to."""
    aircraft: list[FlagAircraft]
    latitude: float | None = None
    longitude: float | None = None
    """Position of the *nearest matching* aircraft (``aircraft[0]``), so the
    flag sensor can be plotted on the HA Map as one marker per flag (nearest
    military / interesting / …). ``None`` when the feed is empty or unpositioned."""
    photo: PhotoPayload | None = None
    """Planespotters photo of the *nearest matching* aircraft (``aircraft[0]``)
    only, when photos are enabled and one exists — lets a flag card spotlight the
    closest match. ``None`` when the feed is empty or photos are off."""


class AlertPayload(AircraftPayload):
    """The payload published to ``airspace/alert/<rule>/<track_id>``.

    Identical to ``AircraftPayload`` (which now carries the optional ``photo``);
    kept as a distinct type so the alert topic's contract is named separately
    from the wildcard/nearest one and can diverge later without churn.
    """

    @classmethod
    def build(cls, state: AircraftState, photo: PhotoPayload | None = None) -> Self:
        """Project an ``AircraftState`` (+ optional photo) into the alert payload.
        Thin wrapper over ``from_state`` so the two payloads can never drift."""
        return cls.from_state(state, photo=photo)


class ReceiverStatsPayload(BaseModel):
    """Per-receiver stats published to ``airspace/receiver/<name>/stats``.

    HA discovery extracts individual fields via ``value_template`` so
    multiple HA sensors can read from the one stats blob without
    duplicate publishes.
    """

    model_config = ConfigDict(extra="forbid")

    aircraft_count: int
    """Aircraft from the last successful poll."""
    messages_per_sec: float
    """Receiver-reported decoded-message rate. 0.0 if unknown."""
    last_success: datetime | None
    """When the last successful poll completed (UTC). None before
    first success."""
    consecutive_failures: int
    online: bool
    """False after the failure threshold is exceeded; flips back True
    on the next success."""

    @classmethod
    def from_health(cls, health: dict[str, Any]) -> Self:
        """Build from a ``ReceiverSource.health()`` snapshot."""
        return cls(
            aircraft_count=health["aircraft_count"],
            messages_per_sec=health["messages_per_sec"],
            last_success=health["last_success"],
            consecutive_failures=health["consecutive_failures"],
            online=health["online"],
        )


class ReceiverLocationPayload(BaseModel):
    """Per-receiver location published to ``airspace/receiver/<name>/location``.

    Fetched once at startup from the receiver's ``receiver.json`` (or
    overridden by config); republished on every successful broker
    reconnect so HA never loses it.
    """

    model_config = ConfigDict(extra="forbid")

    lat: float
    lon: float
    alt_m: float | None = None
    source: str
    """Provenance: ``"receiver_json"`` | ``"config"`` | ``"default"``."""

    @classmethod
    def from_runtime(cls, location: ReceiverLocation) -> Self:
        return cls(
            lat=location.lat,
            lon=location.lon,
            alt_m=location.alt_m,
            source=location.source,
        )


__all__ = [
    "PAYLOAD_SCHEMA_VERSION",
    "AircraftPayload",
    "AlertPayload",
    "DronePayload",
    "FlagAircraft",
    "FlagFeedPayload",
    "PhotoPayload",
    "ReceiverLocationPayload",
    "ReceiverStatsPayload",
]
