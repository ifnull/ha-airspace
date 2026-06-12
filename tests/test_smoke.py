"""Bootstrap smoke test.

Confirms the package is importable and exposes a version. The version is read
from installed package metadata (driven by pyproject), so this asserts shape,
not a literal — bumping the release version must not break this test.
"""

from __future__ import annotations

import ha_airspace


def test_package_imports() -> None:
    assert isinstance(ha_airspace.__version__, str)
    assert ha_airspace.__version__  # non-empty
