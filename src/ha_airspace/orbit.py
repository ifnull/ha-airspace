"""Orbit / loiter detection (Phase 5).

Adds a derived ``orbiting`` flag to a track that sustains a turn — a police or
surveillance helicopter circling a scene, a loitering drone, an aircraft in a
holding pattern. Because it lands in ``state.flags`` like any other flag, it
composes with the whole pipeline for free: alert rules (``flags: ["orbiting"]``),
``count_by_flag``, flag-transition journaling, and the published payloads.

Method: *signed* cumulative heading change over a sliding window. A straight
track nets ~0; a sustained one-direction turn accumulates toward 360. Using the
signed sum (not the absolute) means zig-zag / S-turn maneuvering cancels out and
does not false-positive, while a racetrack holding pattern still nets ~360 per
circuit. Flag when ``abs(cumulative_turn) >= min_turn_deg`` within ``window_s``.

State is a bounded in-memory per-track heading history (no disk writes); it is
pruned to the window and dropped on purge, so it cannot grow unbounded.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from ha_airspace.config import OrbitConfig
    from ha_airspace.models import AircraftState

ORBIT_FLAG = "orbiting"
"""The reserved flag name this detector adds. Match it in alert rules."""


def _angle_delta(a: float, b: float) -> float:
    """Shortest signed angular difference ``b - a`` in degrees, in (-180, 180].
    Handles the 0/360 wrap (e.g. 350 -> 10 is +20, not -340)."""
    return ((b - a + 180.0) % 360.0) - 180.0


class OrbitDetector:
    """Tracks per-track heading history and flags sustained turns.

    Construction args:
      config: The validated ``OrbitConfig`` (window + turn threshold).
    """

    def __init__(self, config: OrbitConfig) -> None:
        self._window_s = config.window_s
        self._min_turn_deg = config.min_turn_deg
        self._history: dict[str, list[tuple[datetime, float]]] = {}

    def update(self, state: AircraftState) -> None:
        """Sample this track's current heading, prune to the window, and add the
        ``orbiting`` flag to ``state.flags`` when the windowed cumulative turn
        crosses the threshold. Call after enrichment (which reassigns flags) so
        the flag survives to publish/alerts.

        Skips sampling when there is no heading or the aircraft is on the ground
        (taxiing spins the heading and would false-positive)."""
        heading = state.canonical.track_deg
        if heading is None or state.canonical.on_ground:
            return

        points = self._history.setdefault(state.track_id, [])
        # Dedup: multiple receivers can ingest the same track in one poll; only
        # add a sample when time has advanced.
        if not points or points[-1][0] < state.last_seen:
            points.append((state.last_seen, heading))

        cutoff = points[-1][0] - timedelta(seconds=self._window_s)
        while len(points) > 1 and points[0][0] < cutoff:
            points.pop(0)

        if _cumulative_turn(points) >= self._min_turn_deg:
            state.flags.add(ORBIT_FLAG)

    def forget(self, track_id: str) -> None:
        """Drop a track's heading history (call on purge). Idempotent."""
        self._history.pop(track_id, None)


def _cumulative_turn(points: list[tuple[datetime, float]]) -> float:
    """Absolute value of the signed sum of consecutive heading deltas."""
    total = 0.0
    for (_, a), (_, b) in pairwise(points):
        total += _angle_delta(a, b)
    return abs(total)


__all__ = ["ORBIT_FLAG", "OrbitDetector"]
