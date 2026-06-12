"""Multi-source ADS-B enrichment service.

Consumes aircraft.json from one or more dump1090 / readsb / dump978-fa receivers,
joins against reference databases, applies tagging/alert rules, and publishes to
MQTT for Home Assistant consumption.

See DESIGN.md at the repo root for architecture; CLAUDE.md for conventions.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed package metadata (driven by the
    # pyproject version baked into the wheel at build time).
    __version__ = version("ha-airspace")
except PackageNotFoundError:  # bare source tree, not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
