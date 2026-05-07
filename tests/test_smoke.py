"""Bootstrap smoke test.

Confirms the package is importable and version is exposed. Will be replaced
with real coverage as Phase 1 modules ship.
"""

from __future__ import annotations

import adsb_enrich


def test_package_imports() -> None:
    assert adsb_enrich.__version__ == "0.0.0.0"
