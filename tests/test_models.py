"""Tests for ha_airspace.models.

Coverage targets every public surface plus the lifecycle boundary cases —
the merger and publisher both branch on lifecycle and a one-second drift
in the threshold semantics produces zombie or vanishing entities in HA.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from ha_airspace.models import (
    AircraftObservation,
    AircraftState,
    Lifecycle,
    ReceiverLocation,
    Watchpoint,
    parse_callsign,
    parse_hex,
)

# ---------------------------------------------------------------------------
# parse_hex
# ---------------------------------------------------------------------------


class TestParseHex:
    def test_lowercase_icao_passes_through(self) -> None:
        assert parse_hex("ae0001") == ("ae0001", False)

    def test_uppercase_normalized_to_lower(self) -> None:
        assert parse_hex("AE0001") == ("ae0001", False)

    def test_mixed_case_normalized(self) -> None:
        assert parse_hex("AeF1c2") == ("aef1c2", False)

    def test_tisb_prefix_stripped_and_flagged(self) -> None:
        # dump1090 emits leading ~ for TIS-B / ADS-R broadcasts.
        assert parse_hex("~ae0001") == ("ae0001", True)

    def test_tisb_prefix_with_uppercase(self) -> None:
        assert parse_hex("~AE0001") == ("ae0001", True)

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_hex("")

    def test_lone_tilde_raises(self) -> None:
        # After stripping ~, nothing left.
        with pytest.raises(ValueError, match="just a tilde"):
            parse_hex("~")

    def test_non_hex_chars_raise(self) -> None:
        with pytest.raises(ValueError, match="not a valid hex"):
            parse_hex("ghijkl")

    def test_non_hex_with_tilde_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid hex"):
            parse_hex("~ghijkl")

    def test_partial_hex_with_punctuation_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid hex"):
            parse_hex("ae00-01")


# ---------------------------------------------------------------------------
# parse_callsign
# ---------------------------------------------------------------------------


class TestParseCallsign:
    def test_none_passes_through(self) -> None:
        assert parse_callsign(None) is None

    def test_empty_string_returns_none(self) -> None:
        # Absent callsigns must serialize to JSON null, not "".
        assert parse_callsign("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert parse_callsign("        ") is None

    def test_dump1090_padded_callsign_stripped(self) -> None:
        # dump1090 pads to 8 chars with trailing spaces.
        assert parse_callsign("RCH171  ") == "RCH171"

    def test_lowercase_uppercased(self) -> None:
        assert parse_callsign("rch171") == "RCH171"

    def test_mixed_case_uppercased(self) -> None:
        assert parse_callsign("Rch171") == "RCH171"

    def test_internal_whitespace_preserved(self) -> None:
        # "N1 23AB" is unusual but legal — strip outer whitespace, leave inner.
        assert parse_callsign("  N1 23AB  ") == "N1 23AB"


# ---------------------------------------------------------------------------
# Watchpoint
# ---------------------------------------------------------------------------


class TestWatchpoint:
    def test_construction_with_required_fields(self) -> None:
        wp = Watchpoint(name="home", lat=30.33, lon=-97.99)
        assert wp.name == "home"
        assert wp.lat == 30.33
        assert wp.lon == -97.99
        assert wp.elevation_m is None

    def test_construction_with_elevation(self) -> None:
        wp = Watchpoint(name="home", lat=30.33, lon=-97.99, elevation_m=200.0)
        assert wp.elevation_m == 200.0

    def test_frozen_assignment_raises(self) -> None:
        wp = Watchpoint(name="home", lat=30.33, lon=-97.99)
        with pytest.raises(FrozenInstanceError):
            wp.lat = 0.0  # type: ignore[misc]

    def test_hashable_for_set_membership(self) -> None:
        # Frozen dataclasses are hashable; useful for dedup in config validation.
        a = Watchpoint(name="home", lat=30.33, lon=-97.99)
        b = Watchpoint(name="home", lat=30.33, lon=-97.99)
        assert {a, b} == {a}


# ---------------------------------------------------------------------------
# ReceiverLocation
# ---------------------------------------------------------------------------


class TestReceiverLocation:
    def test_construction_minimal(self) -> None:
        loc = ReceiverLocation(lat=30.33, lon=-97.99)
        assert loc.alt_m is None
        assert loc.source == "unknown"

    def test_construction_full(self) -> None:
        loc = ReceiverLocation(lat=30.33, lon=-97.99, alt_m=200.0, source="receiver_json")
        assert loc.alt_m == 200.0
        assert loc.source == "receiver_json"

    def test_frozen(self) -> None:
        loc = ReceiverLocation(lat=30.33, lon=-97.99)
        with pytest.raises(FrozenInstanceError):
            loc.lat = 0.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AircraftObservation
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Deterministic, tz-aware timestamp for use in tests that don't need
    freezegun. Avoid datetime.now() (CLAUDE.md: time is a fixture)."""
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class TestAircraftObservation:
    def test_construction_with_only_required_fields(self) -> None:
        obs = AircraftObservation(
            hex="ae0001",
            observed_at=_now(),
            seen_by="rx-home",
            band="1090",
        )
        assert obs.hex == "ae0001"
        assert obs.band == "1090"
        # Optional fields default to None / False.
        assert obs.lat is None
        assert obs.flight is None
        assert obs.is_tisb is False
        assert obs.nic is None

    def test_construction_full(self) -> None:
        obs = AircraftObservation(
            hex="ae0001",
            observed_at=_now(),
            seen_by="rx-home",
            band="1090",
            flight="RCH171",
            lat=30.33,
            lon=-97.99,
            alt_baro_ft=37000,
            ground_speed_kt=480.5,
            track_deg=270.0,
            vertical_rate_fpm=0,
            on_ground=False,
            rssi_dbfs=-12.3,
            seen_pos_age_s=0.5,
            seen_age_s=0.5,
            nic=8,
            nac_p=10,
            category="A4",
            is_tisb=False,
        )
        assert obs.flight == "RCH171"
        assert obs.nic == 8

    def test_band_is_required_no_default(self) -> None:
        # If someone forgets to pass band, construction must fail loudly.
        # Phase 1 ships single-band so a default would silently miscategorize.
        with pytest.raises(TypeError, match="band"):
            AircraftObservation(  # type: ignore[call-arg]
                hex="ae0001",
                observed_at=_now(),
                seen_by="rx-home",
            )

    def test_frozen_assignment_raises(self) -> None:
        obs = AircraftObservation(hex="ae0001", observed_at=_now(), seen_by="rx-home", band="1090")
        with pytest.raises(FrozenInstanceError):
            obs.flight = "EVAC"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AircraftState
# ---------------------------------------------------------------------------


class TestAircraftStateConstruction:
    def test_from_first_observation(self) -> None:
        obs = AircraftObservation(
            hex="ae0001",
            observed_at=_now(),
            seen_by="rx-home",
            band="1090",
            lat=30.33,
            lon=-97.99,
        )
        state = AircraftState.from_first_observation(obs)

        assert state.hex == "ae0001"
        assert state.first_seen == _now()
        assert state.last_seen == _now()
        assert state.seen_by == {"rx-home"}
        assert state.bands == {"1090"}
        assert state.canonical is obs
        assert state.canonical_source == "rx-home"
        assert state.by_receiver == {"rx-home": obs}

        # Enrichment defaults are empty containers (not the same object).
        assert state.flags == set()
        assert state.db_metadata == {}
        assert state.distance_to == {}
        assert state.bearing_to == {}

        # Predictive schema reserved but None pre-Phase-5.
        assert state.predicted_eta_to_home_s is None
        assert state.predicted_closest_approach_nm is None

    def test_collections_are_independent_per_instance(self) -> None:
        # default_factory must not share state across instances.
        obs1 = AircraftObservation(hex="aaaaaa", observed_at=_now(), seen_by="rx-a", band="1090")
        obs2 = AircraftObservation(hex="bbbbbb", observed_at=_now(), seen_by="rx-b", band="1090")
        s1 = AircraftState.from_first_observation(obs1)
        s2 = AircraftState.from_first_observation(obs2)

        s1.flags.add("military")
        s1.distance_to["home"] = 12.5
        assert s2.flags == set()
        assert s2.distance_to == {}

    def test_state_is_mutable(self) -> None:
        # The merger updates state in place — must not be frozen.
        obs = AircraftObservation(hex="ae0001", observed_at=_now(), seen_by="rx-home", band="1090")
        state = AircraftState.from_first_observation(obs)
        state.last_seen = _now() + timedelta(seconds=1)
        state.seen_by.add("rx-other")
        assert "rx-other" in state.seen_by


class TestAircraftStateLifecycle:
    """Boundary coverage: ACTIVE / STALE / PURGED transitions matter because
    the merger and publisher branch on them. Off-by-one here means HA either
    sees zombie aircraft after they've left coverage or sees them blink at
    5-second intervals.
    """

    @pytest.fixture
    def state(self) -> AircraftState:
        obs = AircraftObservation(hex="ae0001", observed_at=_now(), seen_by="rx-home", band="1090")
        return AircraftState.from_first_observation(obs)

    def test_active_at_zero_age(self, state: AircraftState) -> None:
        assert state.lifecycle(_now()) is Lifecycle.ACTIVE

    def test_active_just_below_stale_threshold(self, state: AircraftState) -> None:
        now = _now() + timedelta(seconds=4.99)
        assert state.lifecycle(now) is Lifecycle.ACTIVE

    def test_active_at_exactly_stale_threshold(self, state: AircraftState) -> None:
        # Inclusive boundary: at exactly stale_after_s, still ACTIVE.
        now = _now() + timedelta(seconds=5.0)
        assert state.lifecycle(now) is Lifecycle.ACTIVE

    def test_stale_just_past_threshold(self, state: AircraftState) -> None:
        now = _now() + timedelta(seconds=5.001)
        assert state.lifecycle(now) is Lifecycle.STALE

    def test_stale_well_into_window(self, state: AircraftState) -> None:
        now = _now() + timedelta(seconds=30.0)
        assert state.lifecycle(now) is Lifecycle.STALE

    def test_stale_at_exactly_expire_threshold(self, state: AircraftState) -> None:
        # Inclusive boundary: at exactly expire_after_s, still STALE.
        now = _now() + timedelta(seconds=60.0)
        assert state.lifecycle(now) is Lifecycle.STALE

    def test_purged_just_past_expire(self, state: AircraftState) -> None:
        now = _now() + timedelta(seconds=60.001)
        assert state.lifecycle(now) is Lifecycle.PURGED

    def test_purged_far_past_expire(self, state: AircraftState) -> None:
        now = _now() + timedelta(seconds=600.0)
        assert state.lifecycle(now) is Lifecycle.PURGED

    def test_custom_thresholds(self, state: AircraftState) -> None:
        # Different consumers may want different thresholds; merger and
        # publisher should be able to call this with their own values.
        now = _now() + timedelta(seconds=10.0)
        assert state.lifecycle(now, stale_after_s=15.0, expire_after_s=120.0) is Lifecycle.ACTIVE
        assert state.lifecycle(now, stale_after_s=5.0, expire_after_s=20.0) is Lifecycle.STALE
        assert state.lifecycle(now, stale_after_s=2.0, expire_after_s=8.0) is Lifecycle.PURGED


# ---------------------------------------------------------------------------
# Lifecycle enum surface
# ---------------------------------------------------------------------------


class TestLifecycleEnum:
    def test_string_values_are_stable(self) -> None:
        # These strings end up in metrics / logs / topics. Don't change them
        # casually; they're part of the (pre-public) wire contract.
        assert Lifecycle.ACTIVE.value == "active"
        assert Lifecycle.STALE.value == "stale"
        assert Lifecycle.PURGED.value == "purged"

    def test_str_enum_is_string_compatible(self) -> None:
        # StrEnum members ARE strings (via isinstance) and serialize bare.
        assert isinstance(Lifecycle.ACTIVE, str)
        assert f"{Lifecycle.STALE}" == "stale"
