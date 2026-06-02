"""``python -m adsb_enrich`` shim. Delegates to the CLI entry point."""

from __future__ import annotations

import sys

from adsb_enrich.cli import main

if __name__ == "__main__":
    sys.exit(main())
