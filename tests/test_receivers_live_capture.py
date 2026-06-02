"""Parser coverage against real captured receiver output.

The synthetic fixtures (aircraft_basic / aircraft_edge_cases) exercise the
mapping field-by-field. These tests pin behavior against *real* dump1090-fa
output captured from a live PiAware (tests/fixtures/aircraft_live_1090.json,
26 aircraft, full ADS-B v2 field set: nav_modes, sil_type, gva/sda, emergency)
plus a live-but-empty dump978 feed. They guard against schema drift the hand-
written fixtures would not catch — the real wire format carries fields the
parser must ignore without choking.

Captured 2026-06-02 from a real receiver. If a future parser change breaks
these, re-capture and eyeball the diff rather than blindly re-baselining.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adsb_enrich.models import AircraftObservation
from adsb_enrich.receivers._parse import parse_aircraft_json

FIXTURES = Path(__file__).parent / "fixtures"
_RX = "rx-live"


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _parse_fixture(name: str, band: str) -> list[AircraftObservation]:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    observations, _mps = parse_aircraft_json(
        payload, receiver_name=_RX, band=band, observed_at=_now()
    )
    return observations


# ---------------------------------------------------------------------------
# Live 1090 capture
# ---------------------------------------------------------------------------


class TestLive1090:
    @pytest.fixture
    def observations(self) -> list[AircraftObservation]:
        return _parse_fixture("aircraft_live_1090.json", "1090")

    def test_parses_all_aircraft(self, observations: list[AircraftObservation]) -> None:
        # The capture has 26 aircraft, every one a usable record (real
        # dump1090 output, all with valid hex codes).
        assert len(observations) == 26

    def test_every_observation_has_valid_identity(
        self, observations: list[AircraftObservation]
    ) -> None:
        for obs in observations:
            assert obs.hex  # non-empty
            assert obs.hex == obs.hex.lower()  # normalized
            assert obs.seen_by == _RX
            assert obs.band == "1090"

    def test_full_v2_record_maps_known_fields(
        self, observations: list[AircraftObservation]
    ) -> None:
        # SKW6036 is a complete ADS-B v2 record: nav_modes, sil_type=perhour,
        # gva/sda, nic_baro, emergency=none. The parser must extract the
        # fields it knows and ignore the rest without raising.
        skw = next(o for o in observations if o.flight == "SKW6036")
        assert skw.alt_baro_ft == 31000
        assert skw.alt_geom_ft == 32750
        assert skw.ground_speed_kt == 468.0
        assert skw.squawk == "3633"
        assert skw.category == "A3"
        assert skw.nic == 8
        assert skw.nac_p == 10
        assert skw.lat is not None
        assert skw.lon is not None

    def test_callsigns_are_stripped(self, observations: list[AircraftObservation]) -> None:
        # dump1090 right-pads callsigns to 8 chars; none should survive with
        # trailing whitespace.
        for obs in observations:
            if obs.flight is not None:
                assert obs.flight == obs.flight.strip()
                assert obs.flight  # not empty after strip

    def test_positioned_subset_has_coordinates(
        self, observations: list[AircraftObservation]
    ) -> None:
        # Not every aircraft has a position fix; those that do must carry a
        # real lat/lon pair (never one without the other).
        for obs in observations:
            assert (obs.lat is None) == (obs.lon is None)
        assert any(o.lat is not None for o in observations)


# ---------------------------------------------------------------------------
# Live 978 capture (empty — no UAT traffic at capture time)
# ---------------------------------------------------------------------------


class TestLive978Empty:
    def test_empty_feed_parses_to_no_observations(self) -> None:
        # A live dump978 feed with no aircraft is normal, not an error.
        observations = _parse_fixture("aircraft_live_978_empty.json", "978")
        assert observations == []
