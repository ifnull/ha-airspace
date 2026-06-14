"""Tests for ha_airspace.orbit.OrbitDetector.

Deterministic: states are built with explicit headings + timestamps, no clock.
Covers the angle-wrap math, the signed-cumulative-turn detection (circle vs
straight vs zig-zag), window pruning, the on-ground / no-heading skips, and
history cleanup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ha_airspace.config import OrbitConfig
from ha_airspace.models import AircraftObservation, AircraftState
from ha_airspace.orbit import ORBIT_FLAG, OrbitDetector, _angle_delta

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _detector(*, window_s: float = 120.0, min_turn_deg: float = 360.0) -> OrbitDetector:
    return OrbitDetector(OrbitConfig(enabled=True, window_s=window_s, min_turn_deg=min_turn_deg))


def _state(
    *,
    track_id: str = "ae0001",
    heading: float | None,
    at: datetime,
    on_ground: bool = False,
) -> AircraftState:
    obs = AircraftObservation(
        hex=track_id,
        observed_at=at,
        seen_by="rx",
        band="1090",
        track_deg=heading,
        on_ground=on_ground,
    )
    state = AircraftState.from_first_observation(obs)
    state.last_seen = at
    return state


def _feed(det: OrbitDetector, headings: list[float], *, step_s: float = 5.0, **kw: object) -> bool:
    """Feed a heading sequence (one sample per step) into the detector for one
    track; return whether the final state ends up flagged orbiting."""
    state = None
    for i, h in enumerate(headings):
        state = _state(heading=h, at=_T0 + timedelta(seconds=i * step_s), **kw)  # type: ignore[arg-type]
        det.update(state)
    assert state is not None
    return ORBIT_FLAG in state.flags


class TestAngleDelta:
    def test_simple(self) -> None:
        assert _angle_delta(0, 90) == 90
        assert _angle_delta(90, 0) == -90

    def test_wrap(self) -> None:
        assert _angle_delta(350, 10) == 20  # +20 across 0, not -340
        assert _angle_delta(10, 350) == -20

    def test_half_turn(self) -> None:
        assert abs(_angle_delta(0, 180)) == 180


class TestDetection:
    def test_full_circle_flags(self) -> None:
        # 0 -> 90 -> 180 -> 270 -> 0 = +360 in one direction.
        assert _feed(_detector(), [0, 90, 180, 270, 0]) is True

    def test_straight_flight_does_not_flag(self) -> None:
        assert _feed(_detector(), [90, 90, 90, 90, 90]) is False

    def test_zigzag_cancels(self) -> None:
        # Equal-and-opposite turns net ~0 even though raw motion is large.
        assert _feed(_detector(), [0, 90, 0, 90, 0, 90, 0]) is False

    def test_partial_turn_below_threshold(self) -> None:
        # A single 90 course change nets 90 < 360.
        assert _feed(_detector(), [0, 45, 90]) is False

    def test_lower_threshold_catches_loose_loiter(self) -> None:
        # 180 of cumulative turn flags when min_turn_deg is 180.
        assert _feed(_detector(min_turn_deg=180), [0, 90, 180]) is True

    def test_two_loops_still_flags(self) -> None:
        assert _feed(_detector(), [0, 120, 240, 0, 120, 240, 0]) is True


class TestWindow:
    def test_old_samples_pruned_out_of_window(self) -> None:
        # Half a circle long ago, then straight flight now: the old turn ages
        # out of the window, so the recent straight leg is not orbiting.
        det = _detector(window_s=30.0, min_turn_deg=180.0)
        # t=0..20: accumulate 180 of turn
        for i, h in enumerate([0, 90, 180]):
            det.update(_state(heading=h, at=_T0 + timedelta(seconds=i * 10)))
        # t=60+: straight, well past the 30s window -> old turn dropped
        last = None
        for i in range(4):
            last = _state(heading=270, at=_T0 + timedelta(seconds=60 + i * 5))
            det.update(last)
        assert last is not None
        assert ORBIT_FLAG not in last.flags


class TestSkips:
    def test_no_heading_not_sampled(self) -> None:
        # A state with no heading must not be sampled or flagged.
        det = _detector(min_turn_deg=10.0)
        s = _state(heading=None, at=_T0)
        det.update(s)
        assert ORBIT_FLAG not in s.flags

    def test_on_ground_not_sampled(self) -> None:
        # A taxiing aircraft spins its heading; must not be called orbiting.
        det = _detector(min_turn_deg=180.0)
        assert _feed(det, [0, 90, 180, 270, 0], on_ground=True) is False


class TestForget:
    def test_forget_drops_history(self) -> None:
        det = _detector(min_turn_deg=180.0)
        for i, h in enumerate([0, 90, 180]):
            det.update(_state(heading=h, at=_T0 + timedelta(seconds=i * 5)))
        det.forget("ae0001")
        # After forget, a fresh straight sample starts a new history -> not orbiting.
        s = _state(heading=270, at=_T0 + timedelta(seconds=100))
        det.update(s)
        assert ORBIT_FLAG not in s.flags

    def test_forget_unknown_is_noop(self) -> None:
        _detector().forget("never-seen")  # must not raise
