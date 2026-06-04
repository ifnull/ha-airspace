#!/usr/bin/env python3
"""Live flag watcher — peak-traffic diagnostics, no MQTT / HA needed.

Uses the real ha-airspace library (the same parsers, HttpJsonReceiver, and
Enricher that ship) to poll a live receiver, enrich against the full
Mictronics + ADSBexchange databases, and print flagged aircraft to the
terminal as they appear. Built for sitting and watching during busy traffic.

Run from the repo root:

    uv run python scripts/watch_flags.py
    uv run python scripts/watch_flags.py --url http://192.168.1.8:8080/data/aircraft.json
    uv run python scripts/watch_flags.py --interval 2 --all      # show every flagged hit
    uv run python scripts/watch_flags.py --only military,emergency

The reference DBs (~24 MB gzip'd) download once to a cache dir (default
/tmp/adsb-db-cache) and are reused; pass --refresh to force a re-download.

Flags evaluated (a superset worth watching):
    military       DB mil flag (Mictronics + ADSBex)
    interesting    DB interesting flag (Mictronics)
    pia / ladd     FAA privacy / limited-display (ADSBex + Mictronics)
    emergency      squawk 7500 / 7600 / 7700
    rotorcraft     emitter category A7
    heavy          emitter category A5 (large/heavy)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from ha_airspace.config import EnrichmentConfig, FlagConfig
from ha_airspace.databases import DatabaseStore, parse_adsbexchange, parse_mictronics
from ha_airspace.enrichment import Enricher
from ha_airspace.models import AircraftState
from ha_airspace.receivers import HttpJsonReceiver

DEFAULT_URL = "http://192.168.1.8:8080/data/aircraft.json"
_MICTRONICS_URL = "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
_ADSBEX_URL = "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz"

# Watch set — broad on purpose; filter at runtime with --only.
_FLAGS: dict[str, FlagConfig] = {
    "military": FlagConfig(sources=["adsbexchange:mil", "mictronics:mil"]),
    "interesting": FlagConfig(sources=["mictronics:interesting"]),
    "pia": FlagConfig(sources=["adsbexchange:pia", "mictronics:pia"]),
    "ladd": FlagConfig(sources=["adsbexchange:ladd", "mictronics:ladd"]),
    "emergency": FlagConfig(squawks=["7500", "7600", "7700"]),
    "rotorcraft": FlagConfig(categories=["A7"]),
    "heavy": FlagConfig(categories=["A5"]),
}

# ANSI colors for the terminal; military is the headline so it gets bold red.
_COLORS = {
    "military": "\033[1;31m",  # bold red
    "emergency": "\033[1;33m",  # bold yellow
    "interesting": "\033[36m",  # cyan
    "pia": "\033[35m",  # magenta
    "ladd": "\033[35m",  # magenta
    "rotorcraft": "\033[32m",  # green
    "heavy": "\033[90m",  # grey
}
_RESET = "\033[0m"


def _cache_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / name


# ADSBexchange 403s the default Python user-agent; send a browser-like one.
_UA = "Mozilla/5.0 (X11; Linux x86_64) ha-airspace/watch"


def _download(url: str, dest: Path) -> bool:
    """Download to dest. Returns True on success; on failure prints a warning,
    leaves any existing cached copy intact, and returns False (non-fatal so a
    single dead source does not abort the watch)."""
    name = url.rsplit("/", maxsplit=1)[-1]
    print(f"  downloading {name} ...", end="", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        cached = " (using cached copy)" if dest.exists() else " (no cache — source skipped)"
        print(f" FAILED: {exc}{cached}")
        return False
    dest.write_bytes(data)
    print(f" {dest.stat().st_size // 1024} KB")
    return True


def load_databases(cache_dir: Path, *, refresh: bool) -> DatabaseStore:
    cache_dir.mkdir(parents=True, exist_ok=True)
    mic = _cache_path(cache_dir, "mictronics.csv.gz")
    adsbex = _cache_path(cache_dir, "adsbexchange.json.gz")
    print("Loading reference databases...")
    if refresh or not mic.exists():
        _download(_MICTRONICS_URL, mic)
    if refresh or not adsbex.exists():
        _download(_ADSBEX_URL, adsbex)

    t = time.time()
    merged: dict[str, dict[str, object]] = {}
    # Mictronics first (lower priority), then ADSBex overwrites on conflict.
    # Either source may be missing (download failed, no cache) — parse what
    # is on disk and warn about the rest rather than aborting.
    if mic.exists():
        for hex_code, entry in parse_mictronics(mic.read_bytes()).items():
            merged[hex_code] = dict(entry)
    else:
        print("  WARNING: no Mictronics DB available")
    if adsbex.exists():
        for hex_code, entry in parse_adsbexchange(adsbex.read_bytes()).items():
            merged.setdefault(hex_code, {}).update(entry)
    else:
        print("  WARNING: no ADSBexchange DB available (mil/pia/ladd reduced)")

    store = DatabaseStore()
    store.swap(merged)
    if not merged:
        print("  ERROR: no reference data loaded — DB-backed flags will not match")
    else:
        print(f"  {len(merged):,} aircraft loaded in {time.time() - t:.1f}s\n")
    return store


def _fmt(state: AircraftState, wanted: set[str]) -> str:
    flags = sorted(state.flags & wanted)
    color = next((_COLORS[f] for f in ("military", "emergency", "interesting") if f in flags), "")
    meta = state.db_metadata
    reg = str(meta.get("reg") or "")
    model = str(meta.get("model") or meta.get("type") or "")
    flight = (state.canonical.flight or "").strip()
    dist = state.distance_to.get("home")
    dist_s = f"{dist:5.1f}nm" if dist is not None else "   --  "
    alt = state.canonical.alt_baro_ft
    alt_s = f"{alt:>6}ft" if alt is not None else "  --   "
    flagstr = ",".join(flags)
    # No leading indent here — the caller adds its own prefix. Color wraps only
    # the content, so a caller can lstrip/prefix without tripping over the ANSI
    # escape that would otherwise sit before the leading spaces.
    line = f"{state.hex}  {flight:<8s} {dist_s} {alt_s}  {reg:<10s} {model:<18s} [{flagstr}]"
    return f"{color}{line}{_RESET}" if color else line


async def watch(args: argparse.Namespace) -> int:
    wanted = {f.strip() for f in args.only.split(",")} if args.only else set(_FLAGS)
    unknown = wanted - set(_FLAGS)
    if unknown:
        print(f"unknown flags in --only: {sorted(unknown)}; valid: {sorted(_FLAGS)}")
        return 2
    flags = {name: cfg for name, cfg in _FLAGS.items() if name in wanted}

    store = load_databases(Path(args.cache_dir), refresh=args.refresh)
    enricher = Enricher(EnrichmentConfig(flags=flags), db_store=store)
    receiver = HttpJsonReceiver("watch", "1090", args.url)

    print(f"Watching {args.url} every {args.interval}s — Ctrl-C to stop")
    print(f"Flags: {', '.join(sorted(wanted))}\n")
    seen_hits: set[str] = set()
    try:
        while True:
            observations = await receiver.fetch()
            hits = []
            for obs in observations:
                state = AircraftState.from_first_observation(obs)
                enricher.enrich(state)
                if state.flags & wanted:
                    hits.append(state)
            hits.sort(key=lambda s: s.distance_to.get("home", 1e9))

            stamp = datetime.now(UTC).strftime("%H:%M:%S")
            new = [h for h in hits if h.hex not in seen_hits]
            if args.all:
                print(f"[{stamp}] {len(observations)} aircraft, {len(hits)} flagged:")
                for h in hits:
                    print(f"  {_fmt(h, wanted)}")  # indented under the header
            else:
                # Quiet mode: only announce newly-seen flagged aircraft.
                for h in new:
                    print(f"[{stamp}] {_fmt(h, wanted)}")
            seen_hits = {h.hex for h in hits}
            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        await receiver.aclose()


def main() -> int:
    p = argparse.ArgumentParser(description="Live flag watcher for ha-airspace.")
    p.add_argument("--url", default=DEFAULT_URL, help="receiver aircraft.json URL")
    p.add_argument("--interval", type=float, default=2.0, help="poll seconds")
    p.add_argument(
        "--all",
        action="store_true",
        help="reprint the full flagged list every poll (default: announce new hits only)",
    )
    p.add_argument("--only", default="", help="comma-separated subset of flags to watch")
    p.add_argument("--cache-dir", default="/tmp/adsb-db-cache", help="DB cache directory")
    p.add_argument("--refresh", action="store_true", help="force re-download of the DBs")
    args = p.parse_args()
    return asyncio.run(watch(args))


if __name__ == "__main__":
    sys.exit(main())
