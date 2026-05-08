"""Pydantic models for the JSON payloads published to MQTT.

These ARE the external API surface — what HA, Grafana, Node-RED, custom
scripts, and every other consumer sees. They live separately from
``adsb_enrich.models`` (internal runtime types) so the boundary between
"private state" and "public contract" is visible at the import path.

Versioning posture: per the CEO + Daniel-as-primary-user decision, no
``schema_version`` field is included during Phases 1-3. Pydantic still
serves as the canonical serializer (consistency, type safety) but the
shape is allowed to evolve freely. Schema lock + ``schema_version: 1``
is a Phase 4 prerequisite alongside Docker / add-on packaging.

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

from adsb_enrich.models import AircraftState, ReceiverLocation


class AircraftPayload(BaseModel):
    """Serialized form of an ``AircraftState`` for the
    ``adsb/aircraft/<hex>`` topic.

    Power-user wildcard topic only — never auto-discovered as HA
    entities (would explode HA's entity registry). Subscribed to
    directly by Grafana, Node-RED, custom scripts.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Identity ------------------------------------------------------
    hex: str
    flight: str | None = None
    registration: str | None = None
    squawk: str | None = None

    # --- Position ------------------------------------------------------
    lat: float | None = None
    lon: float | None = None
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

    @classmethod
    def from_state(cls, state: AircraftState) -> Self:
        """Project an internal ``AircraftState`` into the published
        payload shape. Sets are sorted for deterministic JSON output
        (some MQTT consumers cache by message hash; non-deterministic
        ordering causes false cache misses).
        """
        canonical = state.canonical
        return cls(
            hex=state.hex,
            flight=canonical.flight,
            registration=canonical.registration,
            squawk=canonical.squawk,
            lat=canonical.lat,
            lon=canonical.lon,
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
            first_seen=state.first_seen,
            last_seen=state.last_seen,
            distance_to=dict(state.distance_to),
            bearing_to=dict(state.bearing_to),
            predicted_eta_to_home_s=state.predicted_eta_to_home_s,
            predicted_closest_approach_nm=state.predicted_closest_approach_nm,
            flags=sorted(state.flags),
            db_metadata=dict(state.db_metadata),
        )


class ReceiverStatsPayload(BaseModel):
    """Per-receiver stats published to ``adsb/receiver/<name>/stats``.

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
    """Per-receiver location published to ``adsb/receiver/<name>/location``.

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
    "AircraftPayload",
    "ReceiverLocationPayload",
    "ReceiverStatsPayload",
]
