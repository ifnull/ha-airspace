"""remoteid.json (dump3411 / ASTM F3411 Remote ID) -> AircraftObservation.

Maps the drone-detection feed defined in ``FEED.md`` into the same
source-agnostic observation model ADS-B uses, so drones flow through the
merger / enrichment / publish pipeline as ``band="remoteid"`` tracks:

* ``id``        -> ``track_id`` (the merge key); ``hex`` stays ``None``,
  ``non_icao=True`` so it never cross-matches an ICAO aircraft.
* ``lat``/``lon``, ``alt_geom_ft`` -> the shared position fields.
* ``gs``/``track``/``geom_rate`` -> ``ground_speed_kt`` / ``track_deg`` /
  ``vertical_rate_fpm`` (the feed is already in imperial — the producer
  converts, per FEED.md).
* ``rssi`` -> ``rssi_dbfs``; ``seen``/``seen_pos`` -> the staleness fields.
* drone-only data (``id_type``, ``ua_type``, ``agl_ft``, ``rid_source``, and
  the whole ``operator`` block) -> a ``DroneInfo`` on ``obs.drone``.

The feed envelope mirrors dump1090's idioms (``now``, ``messages``, a polled
array) on purpose, so the same ``MessageRateTracker`` derives messages/sec.

What this raises: ``ValueError`` if the document is not a mapping or lacks a
``drones`` array — the receiver wraps that in ``FetchError``. Individual
malformed drone records are skipped, not fatal (one bad entry must not drop
the whole poll), matching the ADS-B parser's tolerance.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from ha_airspace.models import AircraftObservation, DroneInfo

if TYPE_CHECKING:
    from ha_airspace.receivers._parse import MessageRateTracker

log = structlog.get_logger(__name__)

_BAND = "remoteid"


def parse_remoteid_json(
    payload: Any,
    *,
    receiver_name: str,
    observed_at: datetime,
    rate_tracker: MessageRateTracker | None = None,
) -> tuple[list[AircraftObservation], float | None]:
    """Parse a ``remoteid.json`` document into drone observations.

    Args:
        payload: Already-decoded JSON (a dict at the top level).
        receiver_name: Stable receiver name; copied into ``seen_by``.
        observed_at: Our poll wall-clock (UTC) — we trust our clock, not the
            feed's ``now``, for staleness (same convention as ADS-B).
        rate_tracker: Optional per-receiver rate tracker; when supplied,
            ``messages_per_sec`` is derived from the envelope ``messages`` /
            ``now`` deltas across polls.

    Returns:
        ``(observations, messages_per_sec)``.

    Raises:
        ValueError: payload is not a mapping, or ``drones`` is missing / not a
            list. The receiver wraps this in ``FetchError``.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(  # noqa: TRY004 — domain convention: ValueError -> FetchError
            f"remoteid.json root must be a mapping, got {type(payload).__name__}"
        )
    drones = payload.get("drones")
    if not isinstance(drones, list):
        raise ValueError(  # noqa: TRY004 — domain convention: ValueError -> FetchError
            "remoteid.json missing 'drones' array (or it is not a list)"
        )

    observations: list[AircraftObservation] = []
    skipped = 0
    for raw in drones:
        if not isinstance(raw, Mapping):
            skipped += 1
            continue
        obs = _parse_one(raw, receiver_name=receiver_name, observed_at=observed_at)
        if obs is None:
            skipped += 1
            continue
        observations.append(obs)

    if skipped:
        log.debug(
            "remoteid_json_skipped_records",
            receiver=receiver_name,
            skipped=skipped,
            kept=len(observations),
        )

    messages_per_sec: float | None = None
    if rate_tracker is not None:
        messages_per_sec = rate_tracker.update(
            _get_int(payload, "messages"), _get_float(payload, "now")
        )
    return observations, messages_per_sec


def _parse_one(
    raw: Mapping[str, Any],
    *,
    receiver_name: str,
    observed_at: datetime,
) -> AircraftObservation | None:
    """Map one drone detection. Returns None if it lacks the required ``id``."""
    uas_id = raw.get("id")
    if not isinstance(uas_id, str) or not uas_id:
        return None
    id_type = _get_str(raw, "id_type") or "unknown"

    operator = raw.get("operator")
    operator = operator if isinstance(operator, Mapping) else {}

    drone = DroneInfo(
        id_type=id_type,
        ua_type=_get_str(raw, "ua_type"),
        self_id=_get_str(raw, "self_id"),
        agl_ft=_get_float(raw, "agl_ft"),
        rid_source=_get_str(raw, "rid_source"),
        operator_lat=_get_float(operator, "lat"),
        operator_lon=_get_float(operator, "lon"),
        operator_id=_get_str(operator, "id"),
        operator_alt_takeoff_ft=_get_float(operator, "alt_takeoff_ft"),
    )

    return AircraftObservation(
        track_id=uas_id,
        hex=None,
        non_icao=True,
        observed_at=observed_at,
        seen_by=receiver_name,
        band=_BAND,
        lat=_get_float(raw, "lat"),
        lon=_get_float(raw, "lon"),
        alt_geom_ft=_get_int(raw, "alt_geom_ft"),
        ground_speed_kt=_get_float(raw, "gs"),
        track_deg=_get_float(raw, "track"),
        vertical_rate_fpm=_get_int(raw, "geom_rate"),
        rssi_dbfs=_get_float(raw, "rssi"),
        seen_age_s=_get_float(raw, "seen"),
        seen_pos_age_s=_get_float(raw, "seen_pos"),
        drone=drone,
    )


# ---------------------------------------------------------------------------
# Type-safe getters (mirror _parse.py): tolerate the wire schema, never raise
# on a single odd field.
# ---------------------------------------------------------------------------


def _get_str(raw: Mapping[str, Any], key: str) -> str | None:
    val = raw.get(key)
    return val if isinstance(val, str) and val else None


def _get_int(raw: Mapping[str, Any], key: str) -> int | None:
    val = raw.get(key)
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    # The feed carries imperial values as floats (e.g. alt 1276.2 ft, vrate
    # 197.0 ft/min). The shared model fields (alt_geom_ft, vertical_rate_fpm)
    # are ints — round to the nearest whole unit rather than dropping the
    # value. Sub-foot / sub-fpm precision is not meaningful here.
    if isinstance(val, float):
        return round(val)
    return None


def _get_float(raw: Mapping[str, Any], key: str) -> float | None:
    val = raw.get(key)
    if isinstance(val, bool):
        return None
    if isinstance(val, int | float):
        return float(val)
    return None


__all__ = ["parse_remoteid_json"]
