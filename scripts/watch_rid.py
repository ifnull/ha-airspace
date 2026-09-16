#!/usr/bin/env python3
"""Print what the dump3411 Remote ID feed is actually broadcasting, per poll.

Diagnostic for "the drone showed up but no alert fired". Alert rules gated on
max_distance_nm / max_closest_approach_nm need a position, and max_alt_agl_ft
needs an altitude — a transmitter sending only Basic ID + Self-ID (no Location
message) yields a track with neither, which correctly matches nothing. This
says, per poll, which of those fields are actually present.

Usage: uv run scripts/watch_rid.py [url]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from typing import Any

DEFAULT_URL = "http://192.168.1.16:8754/data/remoteid.json"


def poll(url: str) -> list[dict[str, Any]]:
    with urllib.request.urlopen(url, timeout=4) as resp:
        payload = json.load(resp)
    drones = payload.get("drones")
    return drones if isinstance(drones, list) else []


def describe(drone: dict[str, Any]) -> str:
    has_pos = drone.get("lat") is not None and drone.get("lon") is not None
    has_alt = drone.get("agl_ft") is not None or drone.get("alt_geom_ft") is not None
    verdict = "ALERTABLE" if has_pos and has_alt else "BASIC-ID-ONLY"
    return (
        f"{drone.get('id')}  [{verdict}]  "
        f"lat={drone.get('lat')} lon={drone.get('lon')} "
        f"agl_ft={drone.get('agl_ft')} alt_geom_ft={drone.get('alt_geom_ft')} "
        f"gs={drone.get('gs')} src={drone.get('rid_source')}"
    )


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"polling {url} — Ctrl-C to stop")
    while True:
        stamp = time.strftime("%H:%M:%S")
        try:
            drones = poll(url)
        except Exception as exc:  # noqa: BLE001 — diagnostic script, keep polling
            print(f"{stamp}  fetch failed: {exc}")
        else:
            if not drones:
                print(f"{stamp}  (no drones)")
            for drone in drones:
                print(f"{stamp}  {describe(drone)}")
        sys.stdout.flush()
        time.sleep(2)


if __name__ == "__main__":
    main()
