"""Reference-database subsystem (Phase 2a, slice 2).

Downloads, parses, and serves the Mictronics and ADSBexchange aircraft
databases, merged into one in-memory dict keyed by lowercase hex. The
enricher reads this to populate ``AircraftState.db_metadata``, which the
DB-backed flag matchers (``sources: ["adsbexchange:mil"]``) consume.

Public surface:

* ``DatabaseStore`` — holds the current merged dict; atomic swap on refresh.
* ``parse_mictronics`` / ``parse_adsbexchange`` — pure bytes -> dict parsers.
* ``DatabaseLoader`` — async download + refresh orchestration.
"""

from __future__ import annotations

from adsb_enrich.databases.adsbexchange import parse_adsbexchange
from adsb_enrich.databases.loader import DatabaseLoader, DatabaseStore
from adsb_enrich.databases.mictronics import parse_mictronics

__all__ = [
    "DatabaseLoader",
    "DatabaseStore",
    "parse_adsbexchange",
    "parse_mictronics",
]
