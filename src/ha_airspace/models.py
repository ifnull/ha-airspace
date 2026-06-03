"""Domain types for ha-airspace.

Two value categories live here:

* **Per-receiver observations** (``AircraftObservation``) — one snapshot from one
  receiver at one moment. Frozen, immutable, cheap to copy.
* **Canonical merged state** (``AircraftState``) — the deduplicated view the
  merger maintains and the publisher serializes. Mutable, updated in place.

Plus two pure parse helpers (``parse_hex``, ``parse_callsign``) used at
receiver ingest to normalize the wild west of dump1090-variant field shapes.

This module is intentionally policy-free: no merging, no enrichment, no
serialization. Behavior lives in the service modules.

See DESIGN.md §1 (receiver source) and §3 (merger).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Self


class Lifecycle(StrEnum):
    """Classification of an ``AircraftState`` based on the age of its last
    observation. Computed on demand by ``AircraftState.lifecycle``; not stored
    on the state itself, so the merger and the publisher can use different
    thresholds without coupling.
    """

    ACTIVE = "active"
    """Last observation is fresh; publish updates normally."""
    STALE = "stale"
    """Observation is aging; keep republishing the last known position so HA
    dashboards do not blink, with ``seen_age_s`` ticking up."""
    PURGED = "purged"
    """Observation is too old; clear the retained MQTT topic and drop state."""


@dataclass(frozen=True, slots=True)
class Watchpoint:
    """A named geographic point that alert rules and distance/bearing math
    reference.

    Replaces the singular ``home_location`` from earlier drafts so users can
    attach different alert profiles to multiple sites (home, office, etc.).
    Default config has one entry named ``"home"``; rules omit ``watchpoint``
    iff there is exactly one watchpoint named ``home``.
    """

    name: str
    lat: float
    lon: float
    elevation_m: float | None = None
    """Watchpoint elevation above MSL. Used by ``max_alt_agl_ft`` rule
    matching as a v1 approximation (true AGL needs a DEM).
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiverLocation:
    """Where a receiver believes it is. Pulled once at startup from the
    receiver's ``receiver.json`` if present, otherwise from config.
    """

    lat: float
    lon: float
    alt_m: float | None = None
    source: str = "unknown"
    """Provenance: ``"receiver_json"`` | ``"config"`` | ``"default"``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AircraftObservation:
    """A single observation of an aircraft from a single receiver at a moment
    in time.

    Intentionally close to dump1090's ``aircraft.json`` schema so receiver
    mapping is cheap, but normalized: units are explicit in field names,
    missing fields are None (not absent), and provenance (``seen_by``,
    ``observed_at``, ``band``) is first-class.
    """

    # --- Required identity / provenance ---------------------------------
    hex: str
    """ICAO 24-bit address, lowercase, leading ``~`` stripped (see
    ``parse_hex``). The TIS-B / ADS-R distinction is preserved in
    ``is_tisb``."""
    observed_at: datetime
    """When *we* polled (UTC). Not when the receiver saw the message —
    dump1090's ``now`` is the receiver's clock and may skew."""
    seen_by: str
    """Receiver name from config."""
    band: str
    """``"1090"`` or ``"978"``. **Required, no default.** A silent default
    here was the failure mode that dropped 978 traffic in early prototypes.
    """

    # --- Identity ------------------------------------------------------
    flight: str | None = None
    """Callsign, stripped + uppercased; see ``parse_callsign``."""
    registration: str | None = None
    squawk: str | None = None

    # --- Position ------------------------------------------------------
    lat: float | None = None
    lon: float | None = None
    alt_baro_ft: int | None = None
    alt_geom_ft: int | None = None
    nav_altitude_mcp_ft: int | None = None
    """Selected (autopilot target) altitude when broadcast."""

    # --- Movement ------------------------------------------------------
    ground_speed_kt: float | None = None
    track_deg: float | None = None
    vertical_rate_fpm: int | None = None
    on_ground: bool | None = None

    # --- Signal quality (per-receiver, used by Phase 3 merger) ---------
    rssi_dbfs: float | None = None
    seen_pos_age_s: float | None = None
    """How stale this position is, per the receiver's clock."""
    seen_age_s: float | None = None
    """How stale any data for this hex is, per the receiver's clock."""

    # --- Position-quality (Phase 3 canonical-position tiebreaker) ------
    # Captured from Phase 1 onwards even though the tiebreaker only fires
    # in Phase 3 — receivers expose these fields directly and storing
    # them costs nothing.
    nic: int | None = None
    """Navigation Integrity Category (0-11). Aircraft self-reports
    position-integrity quality; primary tiebreaker for canonical pick."""
    nac_p: int | None = None
    """Navigation Accuracy Category, position. Secondary tiebreaker."""

    # --- Type / category from the receiver itself (not DB join) --------
    category: str | None = None
    """ADS-B emitter category, e.g. ``"A3"`` (large fixed-wing),
    ``"A7"`` (rotorcraft), ``"B6"`` (UAV)."""
    aircraft_type: str | None = None
    """ICAO type designator (readsb only)."""

    # --- Provenance flag -----------------------------------------------
    is_tisb: bool = False
    """True if the source broadcast had a leading ``~`` indicating TIS-B
    or ADS-R. Real aircraft, just not broadcasting their own ICAO."""


@dataclass(slots=True, kw_only=True)
class AircraftState:
    """Canonical merged view of an aircraft. Maintained by the merger,
    consumed by the enricher and publisher.

    Mutable on purpose: the merger updates ``last_seen``, ``by_receiver``,
    ``canonical`` etc. as new observations arrive; the enricher writes to
    ``flags``, ``db_metadata``, and the geometry dicts. Fields are
    deliberately decoupled so the merger never touches enrichment results
    and vice versa.
    """

    # --- Identity ------------------------------------------------------
    hex: str
    first_seen: datetime
    last_seen: datetime
    seen_by: set[str]
    """All receiver names that have ever observed this hex (Phase 3+)."""
    bands: set[str]
    """``{"1090"}`` or ``{"978"}`` or both — same hex on both bands is
    the same aircraft (Phase 3 band-merge)."""

    # --- Canonical position ---------------------------------------------
    canonical: AircraftObservation
    """The observation chosen as authoritative for position/movement.
    Phase 1 single-receiver: always the latest observation. Phase 3
    multi-receiver: chosen via NIC → NAC_p → seen_pos_age_s → RSSI →
    receiver name (DESIGN.md §3)."""
    canonical_source: str
    """Receiver name that supplied ``canonical``."""

    # --- Per-receiver detail (Phase 3+) --------------------------------
    by_receiver: dict[str, AircraftObservation]
    """Latest observation from each receiver that has ever seen this hex.
    Used for diagnostics and per-receiver MQTT publishes."""

    # --- Enrichment results (populated by enricher, Phase 2a+) ---------
    flags: set[str] = field(default_factory=set)
    """Symbolic tags produced by flag rules (e.g. ``{"military",
    "interesting"}``). Empty in Phase 1."""
    db_metadata: dict[str, object] = field(default_factory=dict)
    """Merged Mictronics + ADSBex fields (registration, type, operator,
    mil flag, etc.). Schema firms up in Phase 2a; ``object`` valued for
    now since downstream consumers tolerate JSON-equivalent shapes."""
    distance_to: dict[str, float] = field(default_factory=dict)
    """Watchpoint name → great-circle distance in nautical miles."""
    bearing_to: dict[str, float] = field(default_factory=dict)
    """Watchpoint name → bearing in degrees (0=N, 90=E)."""

    # --- Predictive (schema reserved Phase 2c; impl Phase 5) -----------
    # These ship as ``None`` in Phases 1-2c so the published payload shape
    # stays stable when Phase 5 turns on the projection math.
    predicted_eta_to_home_s: float | None = None
    predicted_closest_approach_nm: float | None = None

    @classmethod
    def from_first_observation(cls, obs: AircraftObservation) -> Self:
        """Build a fresh state from the first observation of a hex.

        The single-receiver case (Phase 1) is degenerate: ``canonical`` is
        the only observation. Phase 3 merger may construct via this path
        for new hexes, then immediately update ``canonical`` if a more
        authoritative observation arrives in the same cycle.
        """
        return cls(
            hex=obs.hex,
            first_seen=obs.observed_at,
            last_seen=obs.observed_at,
            seen_by={obs.seen_by},
            bands={obs.band},
            canonical=obs,
            canonical_source=obs.seen_by,
            by_receiver={obs.seen_by: obs},
        )

    def lifecycle(
        self,
        now: datetime,
        *,
        stale_after_s: float = 5.0,
        expire_after_s: float = 60.0,
    ) -> Lifecycle:
        """Classify based on the age of ``last_seen`` relative to ``now``.

        Boundaries are inclusive of ACTIVE / STALE: at exactly
        ``stale_after_s`` the state is still ACTIVE; at exactly
        ``expire_after_s`` it is still STALE. The merger uses this to
        decide whether to keep republishing.
        """
        age = (now - self.last_seen).total_seconds()
        if age > expire_after_s:
            return Lifecycle.PURGED
        if age > stale_after_s:
            return Lifecycle.STALE
        return Lifecycle.ACTIVE


# ---------------------------------------------------------------------------
# Pure parse helpers (called by every receiver implementation at ingest).
# ---------------------------------------------------------------------------

_HEX_CHARS: frozenset[str] = frozenset("0123456789abcdef")


def parse_hex(raw: str) -> tuple[str, bool]:
    """Normalize an ADS-B hex code into ``(bare_hex, is_tisb)``.

    dump1090 prefixes with ``~`` to mark TIS-B / ADS-R broadcasts where
    the hex is not the aircraft's own ICAO 24-bit address. We strip the
    prefix so downstream code keys on the bare hex (same plane on both
    1090 and 978 should merge), but preserve the distinction in the flag.

    Raises:
        ValueError: empty input or non-hex characters after normalization.
    """
    if not raw:
        raise ValueError("hex code is empty")
    is_tisb = raw.startswith("~")
    bare = raw.removeprefix("~").lower()
    if not bare:
        raise ValueError(f"hex code is just a tilde: {raw!r}")
    if any(c not in _HEX_CHARS for c in bare):
        raise ValueError(f"not a valid hex code: {raw!r}")
    return bare, is_tisb


def parse_callsign(raw: str | None) -> str | None:
    """Normalize a callsign field.

    dump1090 pads callsigns to 8 characters with trailing spaces, so a
    naive ``str(field)`` produces ``"RCH171  "``. Strip and uppercase;
    return ``None`` for absent or whitespace-only input so an absent
    callsign serializes as JSON null instead of an empty string.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return stripped.upper()
