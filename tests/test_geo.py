"""Tests for ha_airspace.geo.

Pinned reference: LAX -> JFK is ~2144 NM, initial bearing ~66deg. If
either drifts the cause is either Earth-radius constant change or a
formula bug; both should fail this test loudly.

Tolerances:
  - Distance: abs=0.5 NM at continental ranges (haversine vs Vincenty
    diverge by ~0.5% worst case at the poles; we are well within that).
  - Bearing: abs=0.1deg at compass-cardinal points; abs=1.0deg at
    arbitrary geographic pairs.
"""

from __future__ import annotations

import math

import pytest

from ha_airspace.geo import (
    EARTH_RADIUS_NM,
    bearing,
    closest_point_of_approach,
    haversine,
)

# Canonical airport reference points (DD precision matches FAA database).
LAX = (33.9425, -118.4081)
JFK = (40.6398, -73.7789)


# ---------------------------------------------------------------------------
# haversine
# ---------------------------------------------------------------------------


class TestHaversine:
    def test_zero_distance_for_same_point(self) -> None:
        # Numerically not exactly 0 due to floating point, but well within abs.
        assert haversine(30.33, -97.99, 30.33, -97.99) == pytest.approx(0.0, abs=1e-9)

    def test_lax_to_jfk_reference(self) -> None:
        # Canonical reference: ~2146 NM by spherical haversine with
        # WGS-84 mean radius. Geodesic calculators give ~2151 NM
        # (~0.2% higher); we accept the spherical answer as-is.
        # If this drifts >2 NM the constant or formula has changed.
        nm = haversine(*LAX, *JFK)
        assert nm == pytest.approx(2146.0, abs=2.0)

    def test_symmetric(self) -> None:
        # Distance is direction-independent.
        assert haversine(*LAX, *JFK) == pytest.approx(haversine(*JFK, *LAX), abs=1e-9)

    def test_one_degree_latitude_at_equator(self) -> None:
        # 1 degree of latitude ~= 60 NM by definition of the nautical mile.
        # Tolerance reflects the WGS-84 vs spherical Earth difference.
        nm = haversine(0.0, 0.0, 1.0, 0.0)
        assert nm == pytest.approx(60.0, abs=0.2)

    def test_one_degree_longitude_at_equator(self) -> None:
        # At the equator, 1 degree of longitude ~= 60 NM as well.
        nm = haversine(0.0, 0.0, 0.0, 1.0)
        assert nm == pytest.approx(60.0, abs=0.2)

    def test_one_degree_longitude_shrinks_with_latitude(self) -> None:
        # Meridians converge: 1 degree longitude at 60deg latitude ~= 30 NM
        # (cos(60deg) = 0.5).
        nm = haversine(60.0, 0.0, 60.0, 1.0)
        assert nm == pytest.approx(30.0, abs=0.2)

    def test_antipodal_distance_is_half_circumference(self) -> None:
        # Half the great-circle = pi * R.
        nm = haversine(0.0, 0.0, 0.0, 180.0)
        assert nm == pytest.approx(math.pi * EARTH_RADIUS_NM, abs=0.1)

    def test_north_pole_to_south_pole(self) -> None:
        # Same as antipodal — pi * R.
        nm = haversine(90.0, 0.0, -90.0, 0.0)
        assert nm == pytest.approx(math.pi * EARTH_RADIUS_NM, abs=0.1)

    def test_short_range_alert_distance(self) -> None:
        # Sanity at the 30 NM alert-rule scale: 0.5deg latitude offset
        # near Austin should be ~30 NM.
        nm = haversine(30.33, -97.99, 30.83, -97.99)
        assert nm == pytest.approx(30.0, abs=0.2)


# ---------------------------------------------------------------------------
# bearing
# ---------------------------------------------------------------------------


class TestBearing:
    def test_due_north(self) -> None:
        # Point B directly north of A.
        assert bearing(0.0, 0.0, 1.0, 0.0) == pytest.approx(0.0, abs=1e-6)

    def test_due_east(self) -> None:
        # At the equator, due east is exactly 90deg.
        assert bearing(0.0, 0.0, 0.0, 1.0) == pytest.approx(90.0, abs=1e-6)

    def test_due_south(self) -> None:
        assert bearing(1.0, 0.0, 0.0, 0.0) == pytest.approx(180.0, abs=1e-6)

    def test_due_west_normalized_to_270(self) -> None:
        # The raw atan2 result for due west would be -90deg; the
        # normalization step must wrap it into [0, 360).
        assert bearing(0.0, 1.0, 0.0, 0.0) == pytest.approx(270.0, abs=1e-6)

    def test_zero_bearing_for_same_point(self) -> None:
        # atan2(0, 0) returns 0 by convention; we accept that for the
        # degenerate case rather than raising.
        assert bearing(30.33, -97.99, 30.33, -97.99) == pytest.approx(0.0, abs=1e-6)

    def test_result_always_in_range(self) -> None:
        # Spot-check across all four quadrants that the result stays in
        # [0, 360). This catches a missing modulo step at refactor time.
        for lat2, lon2 in [(1.0, 0.5), (1.0, -0.5), (-1.0, 0.5), (-1.0, -0.5)]:
            b = bearing(0.0, 0.0, lat2, lon2)
            assert 0.0 <= b < 360.0

    def test_lax_to_jfk_reference_bearing(self) -> None:
        # Initial bearing LAX -> JFK is ~66deg (slightly N of due-east).
        # If this drifts the formula has flipped a sign somewhere.
        b = bearing(*LAX, *JFK)
        assert b == pytest.approx(66.0, abs=1.0)

    def test_jfk_to_lax_reverse_bearing(self) -> None:
        # The reverse great-circle bearing is NOT 180+forward — great
        # circles change heading along the path. JFK -> LAX initial
        # bearing is ~273deg (slightly N of due-west).
        b = bearing(*JFK, *LAX)
        assert b == pytest.approx(273.0, abs=1.0)


# ---------------------------------------------------------------------------
# constant
# ---------------------------------------------------------------------------


class TestEarthRadius:
    def test_radius_in_nm_matches_wgs84_mean(self) -> None:
        # 6371.0088 km / 1.852 km/NM == 3440.0653...
        # This is a regression check: don't change the constant casually.
        # (Plain abs() compare instead of pytest.approx to dodge SIM300.)
        assert abs(EARTH_RADIUS_NM - 3440.065) < 0.001


# ---------------------------------------------------------------------------
# closest_point_of_approach (Phase 5 predictive)
# ---------------------------------------------------------------------------

_HOME = (30.0, -97.0)  # lat, lon


class TestClosestPointOfApproach:
    def test_head_on_passes_through_home(self) -> None:
        # Aircraft due south of home, tracking north (000) -> flies straight to it.
        ac_lat, ac_lon = 29.0, -97.0  # 60 nm south
        cpa, eta = closest_point_of_approach(*_HOME, ac_lat, ac_lon, 0.0, 360.0)
        assert cpa == pytest.approx(0.0, abs=1e-6)  # passes through home
        assert eta == pytest.approx(600.0, rel=0.01)  # 60 nm at 360 kt = 600 s

    def test_tangential_cpa_is_perpendicular_offset(self) -> None:
        # Aircraft 60 nm south + 10 nm east of home, tracking due north (000):
        # it stays 10 nm east, so CPA = 10 nm when it's abeam.
        ac_lat = 29.0
        ac_lon = -97.0 + 10.0 / (60.0 * math.cos(math.radians(30.0)))
        cpa, eta = closest_point_of_approach(*_HOME, ac_lat, ac_lon, 0.0, 360.0)
        assert cpa == pytest.approx(10.0, abs=0.05)
        assert eta is not None
        assert eta > 0

    def test_departing_has_no_eta(self) -> None:
        # 60 nm south of home but tracking south (180) -> moving away.
        cpa, eta = closest_point_of_approach(*_HOME, 29.0, -97.0, 180.0, 360.0)
        assert eta is None
        assert cpa == pytest.approx(60.0, rel=0.01)  # current distance

    def test_stationary_has_no_eta(self) -> None:
        cpa, eta = closest_point_of_approach(*_HOME, 29.0, -97.0, 0.0, 0.0)
        assert eta is None
        assert cpa == pytest.approx(60.0, rel=0.01)
