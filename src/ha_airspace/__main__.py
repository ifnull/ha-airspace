"""``python -m ha_airspace`` shim. Delegates to the CLI entry point."""

from __future__ import annotations

import sys

from ha_airspace.cli import main

if __name__ == "__main__":
    sys.exit(main())
