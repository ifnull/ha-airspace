"""dump1090 / readsb / dump978-fa aircraft.json -> AircraftObservation.

Shared between ``HttpJsonReceiver`` (network) and ``FileReceiver``
(disk replay). Differences across the dump1090-fa, dump1090-mutability,
and readsb forks are all in field naming and presence; the parser is
permissive about absent fields and tolerant of extra ones.

What the parser does NOT do:

* Validate hex range or position bounds beyond ``parse_hex``'s
  basic check. Receivers report what they decode; the merger and
  enricher trust the values they get.
* Compute messages-per-second. dump1090's top-level ``messages`` is
  cumulative; deriving a rate needs a delta over a clock, which is
  Phase 1 polish. Returns ``None`` for now.
* Raise on individual malformed records. Skips them silently — one
  garbage entry should not poison the whole poll. Logs at debug for
  visibility.

Messages-per-second: dump1090's top-level ``messages`` is cumulative
since the receiver booted, and ``now`` is the receiver's clock. A rate
is therefore a delta across successive polls — inherently stateful, so
it lives in a ``MessageRateTracker`` the receiver owns and hands to
``parse_aircraft_json``. Without a tracker the parser returns ``None``
(the stateless default).

What it DOES raise: ``ValueError`` if the document does not look like
an aircraft.json at all (no top-level ``aircraft`` array, wrong root
type). ``HttpJsonReceiver``/``FileReceiver`` wrap that in ``FetchError``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import structlog

from adsb_enrich.models import AircraftObservation, parse_callsign, parse_hex

log = structlog.get_logger(__name__)


class MessageRateTracker:
    """Derives messages-per-second from successive cumulative snapshots.

    dump1090/readsb publish a cumulative ``messages`` counter and their
    own ``now`` clock. Rate is ``delta(messages) / delta(now)`` across
    polls. Using the *receiver's* clock for both endpoints makes the
    result immune to skew between our clock and theirs — the skew cancels
    in the delta as long as the receiver's clock is monotonic.

    One instance per receiver; ``update`` is called once per poll. Returns
    ``None`` (not 0.0) whenever a meaningful rate cannot be computed: the
    first sample, a missing field, a non-advancing clock, or a counter
    reset (receiver restart). ``None`` means "unknown"; the publisher
    surfaces the last known stat rather than a misleading zero.
    """

    def __init__(self) -> None:
        self._last_messages: int | None = None
        self._last_now: float | None = None

    def update(self, messages: int | None, now: float | None) -> float | None:
        if messages is None or now is None:
            return None
        prev_messages, prev_now = self._last_messages, self._last_now
        self._last_messages = messages
        self._last_now = now
        if prev_messages is None or prev_now is None:
            return None  # first sample — no delta yet
        dt = now - prev_now
        if dt <= 0:
            return None  # clock did not advance (duplicate poll / time went back)
        delta = messages - prev_messages
        if delta < 0:
            return None  # counter reset — receiver restarted
        return delta / dt


def parse_aircraft_json(
    payload: Any,
    *,
    receiver_name: str,
    band: str,
    observed_at: datetime,
    rate_tracker: MessageRateTracker | None = None,
) -> tuple[list[AircraftObservation], float | None]:
    """Parse a dump1090-style aircraft.json document.

    Args:
        payload: Already-decoded JSON (a dict at the top level).
        receiver_name: Stable receiver name; copied into each
            observation's ``seen_by``.
        band: ``"1090"`` or ``"978"``; copied into each observation.
        observed_at: Wall-clock timestamp the caller supplies (the
            receiver's ``now`` is its clock, which may skew; we trust
            our own per CLAUDE.md).
        rate_tracker: Optional per-receiver ``MessageRateTracker``. When
            supplied, ``messages_per_sec`` is derived from the document's
            cumulative ``messages`` and ``now`` against the previous poll.
            Omit it (the default) to get ``None`` — the stateless path.

    Returns:
        ``(observations, messages_per_sec)``. ``messages_per_sec`` is
        ``None`` unless a ``rate_tracker`` is supplied and a delta is
        available (i.e. not the first poll).

    Raises:
        ValueError: payload is not a mapping, or the ``aircraft`` key
            is missing/wrong type. Receivers wrap this in ``FetchError``.
    """
    # Schema-drift errors raise ValueError, not TypeError, by domain
    # convention: receivers wrap them in FetchError as transient
    # data-shape failures (a corrupt response is not a programmer error).
    if not isinstance(payload, Mapping):
        raise ValueError(  # noqa: TRY004
            f"aircraft.json root must be a mapping, got {type(payload).__name__}"
        )
    aircraft_list = payload.get("aircraft")
    if not isinstance(aircraft_list, list):
        raise ValueError(  # noqa: TRY004
            "aircraft.json missing 'aircraft' array (or it is not a list)"
        )

    observations: list[AircraftObservation] = []
    skipped = 0
    for raw in aircraft_list:
        if not isinstance(raw, Mapping):
            skipped += 1
            continue
        obs = _parse_one(
            raw,
            receiver_name=receiver_name,
            band=band,
            observed_at=observed_at,
        )
        if obs is None:
            skipped += 1
            continue
        observations.append(obs)

    if skipped:
        log.debug(
            "aircraft_json_skipped_records",
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
    band: str,
    observed_at: datetime,
) -> AircraftObservation | None:
    """Parse one aircraft record. Returns None if it is unusable
    (missing or malformed hex)."""
    raw_hex = raw.get("hex")
    if not isinstance(raw_hex, str) or not raw_hex:
        return None
    try:
        hex_code, is_tisb = parse_hex(raw_hex)
    except ValueError:
        return None

    # alt_baro is special: int (feet) when airborne, the literal string
    # "ground" when on the ground. dump1090 has used this convention
    # forever; surface it as on_ground=True with alt_baro_ft=None so
    # downstream code does not have to special-case the string.
    alt_baro_raw = raw.get("alt_baro")
    on_ground = alt_baro_raw == "ground"
    alt_baro_ft = alt_baro_raw if isinstance(alt_baro_raw, int) else None

    return AircraftObservation(
        hex=hex_code,
        observed_at=observed_at,
        seen_by=receiver_name,
        band=band,
        flight=parse_callsign(_get_str(raw, "flight")),
        registration=_get_str(raw, "r"),
        squawk=_get_str(raw, "squawk"),
        lat=_get_float(raw, "lat"),
        lon=_get_float(raw, "lon"),
        alt_baro_ft=alt_baro_ft,
        alt_geom_ft=_get_int(raw, "alt_geom"),
        nav_altitude_mcp_ft=_get_int(raw, "nav_altitude_mcp"),
        ground_speed_kt=_get_float(raw, "gs"),
        track_deg=_get_float(raw, "track"),
        vertical_rate_fpm=_get_int(raw, "baro_rate"),
        on_ground=on_ground if on_ground else None,
        rssi_dbfs=_get_float(raw, "rssi"),
        seen_pos_age_s=_get_float(raw, "seen_pos"),
        seen_age_s=_get_float(raw, "seen"),
        nic=_get_int(raw, "nic"),
        nac_p=_get_int(raw, "nac_p"),
        category=_get_str(raw, "category"),
        aircraft_type=_get_str(raw, "t"),
        is_tisb=is_tisb,
    )


# ---------------------------------------------------------------------------
# Type-safe getters: tolerate dump1090's loose schema without trusting it.
# Returns None instead of raising so a single weird field does not eat the
# whole record.
# ---------------------------------------------------------------------------


def _get_str(raw: Mapping[str, Any], key: str) -> str | None:
    val = raw.get(key)
    return val if isinstance(val, str) else None


def _get_int(raw: Mapping[str, Any], key: str) -> int | None:
    val = raw.get(key)
    # bool is a subclass of int in Python — exclude it explicitly so
    # `nic: True` does not become `nic=1`.
    if isinstance(val, bool):
        return None
    return val if isinstance(val, int) else None


def _get_float(raw: Mapping[str, Any], key: str) -> float | None:
    val = raw.get(key)
    if isinstance(val, bool):
        return None
    if isinstance(val, int | float):
        return float(val)
    return None
