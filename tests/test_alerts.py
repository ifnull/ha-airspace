"""Tests for alert rule evaluation: matching + ENTER/EXIT + cooldown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ha_airspace.alerts import AlertEvaluator, AlertTransition, rule_matches
from ha_airspace.config import AlertRule, AlertsConfig, MatchBlock
from ha_airspace.models import AircraftObservation, AircraftState

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _state(
    hex_code: str = "ae0001",
    *,
    flags: set[str] | None = None,
    category: str | None = None,
    distance_home: float | None = None,
    alt_baro_ft: int | None = None,
    vertical_rate_fpm: int | None = None,
    cpa_nm: float | None = None,
    eta_s: float | None = None,
    prior_last_seen: datetime | None = None,
) -> AircraftState:
    obs = AircraftObservation(
        hex=hex_code,
        observed_at=_T0,
        seen_by="rx",
        band="1090",
        category=category,
        alt_baro_ft=alt_baro_ft,
        vertical_rate_fpm=vertical_rate_fpm,
    )
    state = AircraftState.from_first_observation(obs)
    if flags:
        state.flags = set(flags)
    if distance_home is not None:
        state.distance_to["home"] = distance_home
    state.predicted_closest_approach_nm = cpa_nm
    state.predicted_eta_to_home_s = eta_s
    state.prior_last_seen = prior_last_seen
    return state


def _no_elevation(_name: str) -> float | None:
    return None


# ---------------------------------------------------------------------------
# rule_matches — pure AND/OR semantics
# ---------------------------------------------------------------------------


class TestRuleMatches:
    def test_flags_or_within_key(self) -> None:
        match = MatchBlock(flags=["military", "interesting"])
        assert rule_matches(_state(flags={"interesting"}), match, elevation_m_for=_no_elevation)
        assert not rule_matches(_state(flags={"civilian"}), match, elevation_m_for=_no_elevation)

    def test_keys_and_across(self) -> None:
        # flags AND max_distance: both must hold.
        match = MatchBlock(flags=["military"], max_distance_nm=30, watchpoint="home")
        near_mil = _state(flags={"military"}, distance_home=10)
        far_mil = _state(flags={"military"}, distance_home=50)
        near_civ = _state(flags={"civilian"}, distance_home=10)
        assert rule_matches(near_mil, match, elevation_m_for=_no_elevation)
        assert not rule_matches(far_mil, match, elevation_m_for=_no_elevation)
        assert not rule_matches(near_civ, match, elevation_m_for=_no_elevation)

    def test_category_match(self) -> None:
        match = MatchBlock(category=["A7"])
        assert rule_matches(_state(category="A7"), match, elevation_m_for=_no_elevation)
        assert rule_matches(_state(category="a7"), match, elevation_m_for=_no_elevation)
        assert not rule_matches(_state(category="A3"), match, elevation_m_for=_no_elevation)

    def test_distance_missing_position_no_match(self) -> None:
        match = MatchBlock(max_distance_nm=30, watchpoint="home")
        # No distance_to["home"] -> cannot satisfy a distance condition.
        assert not rule_matches(_state(), match, elevation_m_for=_no_elevation)

    def test_max_alt_agl_with_elevation(self) -> None:
        # Watchpoint at 200 m (~656 ft). Aircraft at 2000 ft MSL -> ~1344 AGL.
        match = MatchBlock(max_alt_agl_ft=2000, watchpoint="home")

        def elev(_name: str) -> float | None:
            return 200.0

        low = _state(alt_baro_ft=2000, distance_home=5)
        high = _state(alt_baro_ft=10000, distance_home=5)
        assert rule_matches(low, match, elevation_m_for=elev)
        assert not rule_matches(high, match, elevation_m_for=elev)

    def test_max_alt_agl_missing_altitude_no_match(self) -> None:
        match = MatchBlock(max_alt_agl_ft=2000, watchpoint="home")
        assert not rule_matches(_state(alt_baro_ft=None), match, elevation_m_for=lambda _n: 200.0)


class TestPredictiveAltitude:
    """Inbound (max_closest_approach_nm) rules project altitude over the ETA and
    gate on the *lower* of now-vs-projected, so descents into drone airspace fire."""

    # Drone-conflict-shaped rule: inbound + low. elevation 0 -> AGL == MSL.
    _RULE = MatchBlock(max_closest_approach_nm=10, max_alt_agl_ft=1640, watchpoint="home")

    def test_descending_above_threshold_matches(self) -> None:
        # 4000 ft now, descending 1000 ft/min, 3 min to approach -> ~1000 ft there.
        s = _state(alt_baro_ft=4000, vertical_rate_fpm=-1000, cpa_nm=2.0, eta_s=180.0)
        assert rule_matches(s, self._RULE, elevation_m_for=_no_elevation)

    def test_high_and_level_does_not_match(self) -> None:
        s = _state(alt_baro_ft=4000, vertical_rate_fpm=0, cpa_nm=2.0, eta_s=180.0)
        assert not rule_matches(s, self._RULE, elevation_m_for=_no_elevation)

    def test_currently_low_climber_still_matches(self) -> None:
        # Low now (1000 ft) climbing out — it's a conflict *now*, so it fires.
        s = _state(alt_baro_ft=1000, vertical_rate_fpm=2000, cpa_nm=2.0, eta_s=180.0)
        assert rule_matches(s, self._RULE, elevation_m_for=_no_elevation)

    def test_non_predictive_rule_uses_current_snapshot(self) -> None:
        # No max_closest_approach_nm -> no projection; a high descender is excluded.
        flat = MatchBlock(max_distance_nm=10, max_alt_agl_ft=1640, watchpoint="home")
        s = _state(
            alt_baro_ft=4000, vertical_rate_fpm=-1000, distance_home=5, cpa_nm=2.0, eta_s=180.0
        )
        assert not rule_matches(s, flat, elevation_m_for=_no_elevation)

    def test_missing_vertical_rate_falls_back_to_snapshot(self) -> None:
        s = _state(alt_baro_ft=4000, vertical_rate_fpm=None, cpa_nm=2.0, eta_s=180.0)
        assert not rule_matches(s, self._RULE, elevation_m_for=_no_elevation)


# ---------------------------------------------------------------------------
# AlertEvaluator — ENTER / EXIT
# ---------------------------------------------------------------------------


def _evaluator(*rules: AlertRule, cooldown_s: float = 60.0, clock: Clock) -> AlertEvaluator:
    return AlertEvaluator(
        AlertsConfig(cooldown_s=cooldown_s, rules=list(rules)),
        elevation_m_for=_no_elevation,
        clock=clock,
    )


class TestEnterExit:
    def test_enter_on_first_match(self) -> None:
        clock = Clock(_T0)
        ev = _evaluator(AlertRule(name="mil", match=MatchBlock(flags=["military"])), clock=clock)
        events = ev.evaluate([_state(flags={"military"})], [])
        assert len(events) == 1
        assert events[0].transition is AlertTransition.ENTER
        assert events[0].rule == "mil"
        assert events[0].state is not None  # ENTER carries the aircraft

    def test_no_duplicate_enter_while_active(self) -> None:
        clock = Clock(_T0)
        ev = _evaluator(AlertRule(name="mil", match=MatchBlock(flags=["military"])), clock=clock)
        s = _state(flags={"military"})
        ev.evaluate([s], [])
        clock.advance(1.0)
        events = ev.evaluate([s], [])  # still matching
        assert events == []

    def test_exit_when_stops_matching(self) -> None:
        clock = Clock(_T0)
        ev = _evaluator(AlertRule(name="mil", match=MatchBlock(flags=["military"])), clock=clock)
        ev.evaluate([_state(flags={"military"})], [])
        clock.advance(1.0)
        # Same hex, no longer military.
        events = ev.evaluate([_state(flags=set())], [])
        assert len(events) == 1
        assert events[0].transition is AlertTransition.EXIT
        assert events[0].state is None  # EXIT carries no state

    def test_exit_when_purged(self) -> None:
        clock = Clock(_T0)
        ev = _evaluator(AlertRule(name="mil", match=MatchBlock(flags=["military"])), clock=clock)
        ev.evaluate([_state("ae0001", flags={"military"})], [])
        clock.advance(1.0)
        # Aircraft purged this cycle (gone from states, in purged list).
        events = ev.evaluate([], ["ae0001"])
        assert len(events) == 1
        assert events[0].transition is AlertTransition.EXIT
        assert events[0].track_id == "ae0001"


class TestCooldown:
    def test_reenter_blocked_within_cooldown(self) -> None:
        clock = Clock(_T0)
        ev = _evaluator(
            AlertRule(name="mil", match=MatchBlock(flags=["military"])),
            cooldown_s=60.0,
            clock=clock,
        )
        s = _state(flags={"military"})
        ev.evaluate([s], [])  # ENTER
        clock.advance(1.0)
        ev.evaluate([_state(flags=set())], [])  # EXIT
        clock.advance(10.0)  # within 60s cooldown
        events = ev.evaluate([s], [])  # matches again, but suppressed
        assert events == []

    def test_reenter_allowed_after_cooldown(self) -> None:
        clock = Clock(_T0)
        ev = _evaluator(
            AlertRule(name="mil", match=MatchBlock(flags=["military"])),
            cooldown_s=60.0,
            clock=clock,
        )
        s = _state(flags={"military"})
        ev.evaluate([s], [])
        clock.advance(1.0)
        ev.evaluate([_state(flags=set())], [])  # EXIT
        clock.advance(61.0)  # past cooldown
        events = ev.evaluate([s], [])
        assert len(events) == 1
        assert events[0].transition is AlertTransition.ENTER


class TestActiveRules:
    def test_active_rules_reflects_membership(self) -> None:
        clock = Clock(_T0)
        ev = _evaluator(
            AlertRule(name="mil", match=MatchBlock(flags=["military"])),
            AlertRule(name="heli", match=MatchBlock(category=["A7"])),
            clock=clock,
        )
        ev.evaluate([_state("ae0001", flags={"military"})], [])
        assert ev.active_rules() == {"mil"}
        clock.advance(1.0)
        ev.evaluate(
            [
                _state("ae0001", flags={"military"}),
                _state("ae0002", category="A7"),
            ],
            [],
        )
        assert ev.active_rules() == {"mil", "heli"}

    def test_multiple_aircraft_one_rule(self) -> None:
        clock = Clock(_T0)
        ev = _evaluator(AlertRule(name="mil", match=MatchBlock(flags=["military"])), clock=clock)
        events = ev.evaluate(
            [_state("ae0001", flags={"military"}), _state("ae0002", flags={"military"})], []
        )
        enters = [e for e in events if e.transition is AlertTransition.ENTER]
        assert {e.track_id for e in enters} == {"ae0001", "ae0002"}
        assert ev.active_rules() == {"mil"}


# ---------------------------------------------------------------------------
# unseen_for_days — history-aware novelty (Phase 5)
# ---------------------------------------------------------------------------


class TestUnseenForDays:
    def test_never_seen_matches(self) -> None:
        # prior_last_seen None = never recorded = most novel -> matches.
        match = MatchBlock(unseen_for_days=30)
        assert rule_matches(
            _state(prior_last_seen=None), match, elevation_m_for=_no_elevation, now=_T0
        )

    def test_seen_long_ago_matches(self) -> None:
        match = MatchBlock(unseen_for_days=30)
        old = _T0 - timedelta(days=40)
        assert rule_matches(
            _state(prior_last_seen=old), match, elevation_m_for=_no_elevation, now=_T0
        )

    def test_seen_recently_does_not_match(self) -> None:
        match = MatchBlock(unseen_for_days=30)
        recent = _T0 - timedelta(days=5)
        assert not rule_matches(
            _state(prior_last_seen=recent), match, elevation_m_for=_no_elevation, now=_T0
        )

    def test_boundary_exactly_n_days_matches(self) -> None:
        # Exactly N days ago counts as "unseen for N days" (>= threshold).
        match = MatchBlock(unseen_for_days=30)
        boundary = _T0 - timedelta(days=30)
        assert rule_matches(
            _state(prior_last_seen=boundary), match, elevation_m_for=_no_elevation, now=_T0
        )

    def test_ands_with_flags(self) -> None:
        # Novel AND military -> matches; novel but not military -> no.
        match = MatchBlock(flags=["military"], unseen_for_days=30)
        old = _T0 - timedelta(days=40)
        assert rule_matches(
            _state(flags={"military"}, prior_last_seen=old),
            match,
            elevation_m_for=_no_elevation,
            now=_T0,
        )
        assert not rule_matches(
            _state(flags={"civilian"}, prior_last_seen=old),
            match,
            elevation_m_for=_no_elevation,
            now=_T0,
        )

    def test_recently_seen_military_does_not_match(self) -> None:
        match = MatchBlock(flags=["military"], unseen_for_days=30)
        recent = _T0 - timedelta(days=1)
        assert not rule_matches(
            _state(flags={"military"}, prior_last_seen=recent),
            match,
            elevation_m_for=_no_elevation,
            now=_T0,
        )


# ---------------------------------------------------------------------------
# Predictive inbound (Phase 5)
# ---------------------------------------------------------------------------


def _inbound_state(cpa: float | None, eta: float | None, **kw: object) -> AircraftState:
    s = _state(**kw)  # type: ignore[arg-type]
    s.predicted_closest_approach_nm = cpa
    s.predicted_eta_to_home_s = eta
    return s


class TestInbound:
    def test_approaching_within_bound_matches(self) -> None:
        match = MatchBlock(max_closest_approach_nm=5)
        assert rule_matches(_inbound_state(3.0, 300.0), match, elevation_m_for=_no_elevation)

    def test_beyond_bound_does_not_match(self) -> None:
        match = MatchBlock(max_closest_approach_nm=5)
        assert not rule_matches(_inbound_state(8.0, 300.0), match, elevation_m_for=_no_elevation)

    def test_departing_never_matches(self) -> None:
        # eta None = departing, even if currently close.
        match = MatchBlock(max_closest_approach_nm=5)
        assert not rule_matches(_inbound_state(2.0, None), match, elevation_m_for=_no_elevation)

    def test_no_prediction_does_not_match(self) -> None:
        match = MatchBlock(max_closest_approach_nm=5)
        assert not rule_matches(_inbound_state(None, None), match, elevation_m_for=_no_elevation)

    def test_within_eta_bound(self) -> None:
        match = MatchBlock(max_closest_approach_nm=5, within_eta_s=600)
        assert rule_matches(_inbound_state(3.0, 300.0), match, elevation_m_for=_no_elevation)
        assert not rule_matches(_inbound_state(3.0, 900.0), match, elevation_m_for=_no_elevation)

    def test_ands_with_flags(self) -> None:
        match = MatchBlock(flags=["military"], max_closest_approach_nm=5)
        approaching_mil = _inbound_state(3.0, 300.0, flags={"military"})
        approaching_civ = _inbound_state(3.0, 300.0, flags={"civilian"})
        assert rule_matches(approaching_mil, match, elevation_m_for=_no_elevation)
        assert not rule_matches(approaching_civ, match, elevation_m_for=_no_elevation)
