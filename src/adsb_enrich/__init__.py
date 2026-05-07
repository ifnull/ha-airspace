"""Multi-source ADS-B enrichment service.

Consumes aircraft.json from one or more dump1090 / readsb / dump978-fa receivers,
joins against reference databases, applies tagging/alert rules, and publishes to
MQTT for Home Assistant consumption.

See DESIGN.md at the repo root for architecture; CLAUDE.md for conventions.
"""

from __future__ import annotations

__version__ = "0.0.0.0"

__all__ = ["__version__"]
