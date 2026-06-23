"""Remote ID spoof detection (Phase 5) — Tier 1.

Adds a derived ``spoof_suspect`` flag to a drone track whose broadcast looks
fabricated. Because it lands in ``state.flags`` like any other flag, it composes
with the whole pipeline for free: alert rules (``flags: ["spoof_suspect"]``),
``count_by_flag``, flag-transition journaling, the flag feed, and the payloads.

**Why behavioral, not identity-based.** Remote ID has no cryptographic
authentication, so a spoofer can rebroadcast *previously-seen real* serials —
which then resolve in the FAA registry. Validating identity therefore cannot
catch a replay; detection has to lean on inconsistency. This module ships the
two cheap, stateless-to-poll-local Tier-1 signals; the kinematic / cross-time
Tier-2 signals layer on the durable sighting store (see DESIGN.md).

Tier-1 signals:

1. **Malformed serial.** ``id_type == "serial"`` claims an ANSI/CTA-2063-A
   serial, but the value isn't shaped like one (length outside 6-20, or not
   alphanumeric) — e.g. the ``0x00`` placeholder a spoofer emits.
2. **Shared self_id across serials.** The same free-text ``self_id`` broadcast by
   two or more *distinct* UAS ids currently in the air. Independent operators
   essentially never type the identical Self-ID string; a replay tool stamps one
   string across the fake serials it emits (the observed case was "Spoofing
   test" across three serials).

State is a small in-memory ``self_id -> {track_ids}`` index for signal 2, pruned
on purge (``forget``), so it cannot grow unbounded. No disk writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ha_airspace.config import SpoofConfig
    from ha_airspace.models import AircraftState

SPOOF_FLAG = "spoof_suspect"
"""The reserved flag name this detector adds. Match it in alert rules."""

# ANSI/CTA-2063-A serial = 4-char manufacturer code + 1 length char + 1-15 char
# manufacturer serial => 6-20 alphanumeric characters. We don't fully validate
# the structure (Tier 2 can); we reject values that can't be a conformant serial
# at all, which is what placeholders/spoofs trip.
_SERIAL_MIN_LEN = 6
_SERIAL_MAX_LEN = 20


def _is_malformed_serial(id_type: str, uas_id: str) -> bool:
    """True when a track claims ``id_type == "serial"`` but the id can't be a
    conformant ANSI/CTA-2063-A serial. Only serials make this claim — session /
    utm_uuid / caa_reg ids have their own formats and are not judged here."""
    if id_type != "serial":
        return False
    return not (_SERIAL_MIN_LEN <= len(uas_id) <= _SERIAL_MAX_LEN and uas_id.isalnum())


class SpoofDetector:
    """Flags drones with fabrication tells. Mirrors ``OrbitDetector``: call
    ``update`` per poll after enrichment (so the derived flag survives the flag
    reassignment), and ``forget`` on purge.

    Construction args:
      config: The validated ``SpoofConfig`` (currently just the on/off gate;
        Tier-2 thresholds will live here).
    """

    def __init__(self, config: SpoofConfig) -> None:
        self._config = config
        # self_id -> set of distinct track_ids currently broadcasting it.
        self._self_id_tracks: dict[str, set[str]] = {}
        # track_id -> the self_id it last broadcast, so a change/forget can prune
        # its entry from the index above.
        self._track_self_id: dict[str, str] = {}

    def update(self, state: AircraftState) -> None:
        """Evaluate the Tier-1 signals for a drone track and add
        ``spoof_suspect`` to ``state.flags`` when any fires. No-op for non-drone
        tracks. Call after ``Enricher.enrich`` (which reassigns ``flags``)."""
        drone = state.canonical.drone
        if drone is None or "remoteid" not in state.bands:
            return

        if _is_malformed_serial(drone.id_type, state.track_id):
            state.flags.add(SPOOF_FLAG)

        if self._self_id_shared(state.track_id, drone.self_id):
            state.flags.add(SPOOF_FLAG)

    def _self_id_shared(self, track_id: str, self_id: str | None) -> bool:
        """Maintain the self_id index for this track and report whether its
        current self_id is also broadcast by at least one *other* track."""
        prev = self._track_self_id.get(track_id)
        sid = self_id or None  # treat "" / None alike: no self_id, no signal
        if prev != sid:
            self._unindex(track_id, prev)
            if sid is not None:
                self._self_id_tracks.setdefault(sid, set()).add(track_id)
                self._track_self_id[track_id] = sid
        if sid is None:
            return False
        return len(self._self_id_tracks.get(sid, ())) > 1

    def _unindex(self, track_id: str, self_id: str | None) -> None:
        if self_id is None:
            return
        tracks = self._self_id_tracks.get(self_id)
        if tracks is not None:
            tracks.discard(track_id)
            if not tracks:
                del self._self_id_tracks[self_id]
        self._track_self_id.pop(track_id, None)

    def forget(self, track_id: str) -> None:
        """Drop a track from the self_id index (call on purge). Idempotent."""
        self._unindex(track_id, self._track_self_id.get(track_id))


__all__ = ["SPOOF_FLAG", "SpoofDetector"]
