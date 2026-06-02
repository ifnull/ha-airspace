"""Tests for evaluate_flags — pure declarative flag-rule evaluation.

Synthetic AircraftStates, no I/O. Covers every matcher type, the
case-insensitivity rules, DB-backed flags before/after db_metadata is
populated, and the empty-config no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adsb_enrich.config import FlagConfig
from adsb_enrich.flags import evaluate_flags
from adsb_enrich.models import AircraftObservation, AircraftState

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _state(
    *,
    hex_code: str = "ae0001",
    flight: str | None = None,
    squawk: str | None = None,
    aircraft_type: str | None = None,
    category: str | None = None,
    db_metadata: dict[str, object] | None = None,
) -> AircraftState:
    obs = AircraftObservation(
        hex=hex_code,
        observed_at=_T0,
        seen_by="rx",
        band="1090",
        flight=flight,
        squawk=squawk,
        aircraft_type=aircraft_type,
        category=category,
    )
    state = AircraftState.from_first_observation(obs)
    if db_metadata is not None:
        state.db_metadata = db_metadata
    return state


# ---------------------------------------------------------------------------
# squawks
# ---------------------------------------------------------------------------


class TestSquawks:
    def test_emergency_squawk_matches(self) -> None:
        flags = {"emergency": FlagConfig(squawks=["7500", "7600", "7700"])}
        assert evaluate_flags(_state(squawk="7700"), flags) == {"emergency"}

    def test_non_matching_squawk(self) -> None:
        flags = {"emergency": FlagConfig(squawks=["7500", "7600", "7700"])}
        assert evaluate_flags(_state(squawk="1200"), flags) == set()

    def test_missing_squawk(self) -> None:
        flags = {"emergency": FlagConfig(squawks=["7700"])}
        assert evaluate_flags(_state(squawk=None), flags) == set()


# ---------------------------------------------------------------------------
# patterns (callsign prefix)
# ---------------------------------------------------------------------------


class TestPatterns:
    def test_prefix_matches(self) -> None:
        flags = {"mil_callsign": FlagConfig(patterns=["RCH", "SAM"])}
        assert evaluate_flags(_state(flight="RCH171"), flags) == {"mil_callsign"}

    def test_prefix_case_insensitive(self) -> None:
        flags = {"mil_callsign": FlagConfig(patterns=["rch"])}
        assert evaluate_flags(_state(flight="RCH171"), flags) == {"mil_callsign"}

    def test_no_prefix_match(self) -> None:
        flags = {"mil_callsign": FlagConfig(patterns=["RCH"])}
        assert evaluate_flags(_state(flight="UAL456"), flags) == set()

    def test_missing_flight(self) -> None:
        flags = {"mil_callsign": FlagConfig(patterns=["RCH"])}
        assert evaluate_flags(_state(flight=None), flags) == set()


# ---------------------------------------------------------------------------
# types (ICAO type designator)
# ---------------------------------------------------------------------------


class TestTypes:
    def test_type_matches_case_insensitive(self) -> None:
        flags = {"heavy_mil": FlagConfig(types=["C17", "B52"])}
        assert evaluate_flags(_state(aircraft_type="c17"), flags) == {"heavy_mil"}

    def test_type_no_match(self) -> None:
        flags = {"heavy_mil": FlagConfig(types=["C17"])}
        assert evaluate_flags(_state(aircraft_type="A320"), flags) == set()


# ---------------------------------------------------------------------------
# categories (ADS-B emitter category)
# ---------------------------------------------------------------------------


class TestCategories:
    def test_category_matches(self) -> None:
        flags = {"rotorcraft": FlagConfig(categories=["A7"])}
        assert evaluate_flags(_state(category="A7"), flags) == {"rotorcraft"}

    def test_category_case_insensitive(self) -> None:
        flags = {"rotorcraft": FlagConfig(categories=["a7"])}
        assert evaluate_flags(_state(category="A7"), flags) == {"rotorcraft"}

    def test_category_no_match(self) -> None:
        flags = {"rotorcraft": FlagConfig(categories=["A7"])}
        assert evaluate_flags(_state(category="A3"), flags) == set()


# ---------------------------------------------------------------------------
# sources (DB-backed)
# ---------------------------------------------------------------------------


class TestSources:
    def test_no_match_when_db_metadata_empty(self) -> None:
        # Before the DB loader (slice 2), db_metadata is {} — never matches.
        flags = {"military": FlagConfig(sources=["adsbexchange:mil"])}
        assert evaluate_flags(_state(), flags) == set()

    def test_matches_when_field_truthy(self) -> None:
        flags = {"military": FlagConfig(sources=["adsbexchange:mil"])}
        state = _state(db_metadata={"mil": True})
        assert evaluate_flags(state, flags) == {"military"}

    def test_no_match_when_field_falsy(self) -> None:
        flags = {"military": FlagConfig(sources=["adsbexchange:mil"])}
        state = _state(db_metadata={"mil": False})
        assert evaluate_flags(state, flags) == set()

    def test_any_source_truthy_matches(self) -> None:
        flags = {"military": FlagConfig(sources=["mictronics:mil", "adsbexchange:mil"])}
        # Only one of the two referenced fields is truthy — still matches (OR).
        state = _state(db_metadata={"mil": True})
        assert evaluate_flags(state, flags) == {"military"}


# ---------------------------------------------------------------------------
# Multiple flags + empty config
# ---------------------------------------------------------------------------


class TestMultiple:
    def test_multiple_flags_independent(self) -> None:
        flags = {
            "emergency": FlagConfig(squawks=["7700"]),
            "rotorcraft": FlagConfig(categories=["A7"]),
            "mil_callsign": FlagConfig(patterns=["RCH"]),
        }
        state = _state(squawk="7700", category="A7", flight="UAL1")
        # emergency + rotorcraft match; callsign does not.
        assert evaluate_flags(state, flags) == {"emergency", "rotorcraft"}

    def test_empty_config_no_flags(self) -> None:
        assert evaluate_flags(_state(squawk="7700"), {}) == set()


# ---------------------------------------------------------------------------
# FlagConfig validation
# ---------------------------------------------------------------------------


class TestFlagConfigValidation:
    def test_exactly_one_matcher_required(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            FlagConfig()

    def test_two_matchers_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            FlagConfig(squawks=["7700"], patterns=["RCH"])

    def test_empty_matcher_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            FlagConfig(squawks=[])

    def test_malformed_source_ref_rejected(self) -> None:
        with pytest.raises(ValueError, match="db:field"):
            FlagConfig(sources=["mil"])  # missing the db: prefix

    def test_matcher_property(self) -> None:
        assert FlagConfig(squawks=["7700"]).matcher == "squawks"
        assert FlagConfig(sources=["a:b"]).matcher == "sources"
