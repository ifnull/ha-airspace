"""Great-circle distance and bearing.

Pure trig, no I/O. Inputs are decimal degrees (the only sensible unit at
this layer; receivers convert from whatever wire format their broadcast
uses). Outputs are nautical miles (matching DESIGN.md ``distance_nm``)
and degrees (0=N, 90=E, normalized to ``[0, 360)``).

We assume system-boundary validation has already happened: callers pass
real lat/lon values from validated receiver observations or watchpoints.
The functions do not range-check; out-of-range inputs produce nonsense
output rather than raising. This matches CLAUDE.md's "validate at system
boundaries, trust internal code" principle.

Vincenty would buy us sub-meter accuracy for an aircraft-tracking use
case where signal-quality dominates. Haversine is the right tradeoff:
~0.5% error worst case (at the poles), microsecond cost, 10 lines.

LAX (33.9425, -118.4081) -> JFK (40.6398, -73.7789) is the canonical
reference: ~2144 NM, initial bearing ~66deg. Tests pin both.
"""

from __future__ import annotations

import math

EARTH_RADIUS_NM: float = 3440.065
"""Mean Earth radius in nautical miles. Source: WGS-84 mean (6371.0088 km)
divided by 1.852 km/NM. Pinned here so the constant is greppable; do not
change without updating the LAX-JFK reference test."""


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in nautical miles.

    Standard haversine formula. Returns 0.0 for identical points (with
    floating-point rounding within ~1e-10 NM).
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_NM * c


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (forward azimuth) from point 1 to point 2, in
    degrees, normalized to ``[0, 360)``.

    Convention: 0 = north, 90 = east, 180 = south, 270 = west. This is
    the *initial* bearing on the great-circle path; the actual heading
    along that path drifts because great circles are not constant-bearing
    routes (loxodromes are). For the ~30 NM ranges in alert rules the
    drift is negligible.

    Returns 0.0 for identical points (atan2(0, 0) convention).
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)

    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    theta = math.atan2(y, x)
    return (math.degrees(theta) + 360) % 360
