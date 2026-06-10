#!/usr/bin/env python3
"""Live airspace watcher — peak-traffic diagnostics, no MQTT / HA needed.

Runs the real ha-airspace pipeline (parsers -> Merger -> geometry -> DB
enrichment -> flags) over the sources in your config and announces flagged
aircraft + cross-source/band merges as they appear. Config is the source of
truth — sources, flags, and alerts all come from it; nothing is hardcoded.

Run from the repo root (auto-discovers config.local.yaml / config.yaml):

    uv run python scripts/watch_flags.py
    uv run python scripts/watch_flags.py --config config.local.yaml
    uv run python scripts/watch_flags.py --only military,emergency   # filter displayed flags
    uv run python scripts/watch_flags.py --url http://host:8080/data/aircraft.json  # override
    uv run python scripts/watch_flags.py --drone-url                  # override the drone feed

The reference DBs (~24 MB gzip'd) download once to a cache dir (default
/tmp/adsb-db-cache) and are reused; pass --refresh to force a re-download.

Which flags exist (and their colors) is defined by `enrichment.flags` in the
config; --only narrows which of those are announced.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ha_airspace.config import Config, load_config
from ha_airspace.databases import DatabaseStore, parse_adsbexchange, parse_mictronics
from ha_airspace.enrichment import Enricher
from ha_airspace.geo import bearing, haversine
from ha_airspace.merger import Merger
from ha_airspace.models import AircraftState
from ha_airspace.receivers import (
    HttpJsonReceiver,
    ReceiverSource,
    RemoteIdHttpReceiver,
)

DEFAULT_DRONE_URL = "http://192.168.1.204:8754/data/remoteid.json"
_MICTRONICS_URL = "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
_ADSBEX_URL = "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz"

# ANSI colors for the terminal; military is the headline so it gets bold red.
# Keys are flag names; coloring only applies to flags a config happens to use.
_COLORS = {
    "military": "\033[1;31m",  # bold red
    "emergency": "\033[1;33m",  # bold yellow
    "mil_squawk": "\033[1;31m",  # bold red (military ops)
    "uas_lost_link": "\033[1;33m",  # bold yellow (drone in trouble)
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


_PRIORITY_COLORS = ("military", "mil_squawk", "emergency", "uas_lost_link", "interesting")


def _fmt(state: AircraftState, wanted: set[str]) -> str:
    flags = sorted(state.flags & wanted)
    color = next((_COLORS[f] for f in _PRIORITY_COLORS if f in flags), "")
    meta = state.db_metadata
    reg = str(meta.get("reg") or "")
    model = str(meta.get("model") or meta.get("type") or "")
    flight = (state.canonical.flight or "").strip()
    squawk = state.canonical.squawk or "----"
    dist = state.distance_to.get("home")
    dist_s = f"{dist:5.1f}nm" if dist is not None else "   --  "
    alt = state.canonical.alt_baro_ft
    alt_s = f"{alt:>6}ft" if alt is not None else "  --   "
    flagstr = ",".join(flags)
    # No leading indent here — the caller adds its own prefix. Color wraps only
    # the content, so a caller can lstrip/prefix without tripping over the ANSI
    # escape that would otherwise sit before the leading spaces.
    line = (
        f"{state.hex}  {flight:<8s} sq:{squawk:<4s} {dist_s} {alt_s}  "
        f"{reg:<10s} {model:<18s} [{flagstr}]"
    )
    return f"{color}{line}{_RESET}" if color else line


_DRONE_COLOR = "\033[1;35m"  # bold magenta — drones stand out from aircraft


def _fmt_drone(state: AircraftState) -> str:
    """One drone line: id, position, native AGL, and operator location if
    known. Like the aircraft view, this watcher does no geometry — the feed's
    own lat/lon/AGL/operator fields are shown directly."""
    d = state.canonical.drone
    lat, lon = state.canonical.lat, state.canonical.lon
    pos = f"{lat:.4f},{lon:.4f}" if lat is not None and lon is not None else "no-fix"
    agl = d.agl_ft if d else None
    agl_s = f"{agl:>6.0f}ft AGL" if agl is not None else "  --      "
    op = ""
    if d and d.operator_lat is not None and d.operator_lon is not None:
        op = f"  operator=({d.operator_lat:.4f},{d.operator_lon:.4f})"
    ua = (d.ua_type if d else None) or "?"
    line = f"DRONE {state.track_id:<20s} {pos:<18s} {agl_s}  {ua}{op}"
    return f"{_DRONE_COLOR}{line}{_RESET}"


_CONFIG_CANDIDATES = ("config.local.yaml", "config.yaml")


def _resolve_config(explicit: str) -> Config:
    """Load the config: an explicit --config path, else auto-discover
    config.local.yaml / config.yaml in the repo root. Config is the source of
    truth for sources, flags, and alerts — the watcher never hardcodes them."""
    if explicit:
        return load_config(explicit)
    root = Path(__file__).resolve().parent.parent
    for name in _CONFIG_CANDIDATES:
        candidate = root / name
        if candidate.is_file():
            print(f"Using config: {candidate.name}")
            return load_config(candidate)
    raise SystemExit(
        "no config found. Pass --config PATH, or create "
        f"{' or '.join(_CONFIG_CANDIDATES)} in the repo root "
        "(copy config.example.yaml)."
    )


def _build_sources(config: Config, args: argparse.Namespace) -> list[ReceiverSource]:
    """Construct receivers from config — ADS-B + Remote ID — so the watcher
    merges exactly what the real service would. CLI flags override config:
    --url replaces the configured 1090 source(s); --drone-url adds/overrides a
    drone feed for a quick spot-check."""
    if args.url:
        # Override: a single ad-hoc 1090 source instead of the config receivers.
        sources: list[ReceiverSource] = [HttpJsonReceiver("watch-1090", "1090", args.url)]
    else:
        sources = [
            HttpJsonReceiver(rc.name, rc.band, rc.url) for rc in config.receivers if rc.enabled
        ]
    if args.drone_url:
        sources.append(RemoteIdHttpReceiver("watch-drone", args.drone_url))
    else:
        sources.extend(
            RemoteIdHttpReceiver(rc.name, rc.url) for rc in config.remoteid if rc.enabled
        )
    return sources


def _fmt_merged(state: AircraftState) -> str:
    """Merge-mode line: emphasizes bands + seen_by so cross-source merges are
    visible. A track seen by >1 receiver or on >1 band is highlighted."""
    bands = "+".join(sorted(state.bands))
    seen = ",".join(sorted(state.seen_by))
    merged = len(state.seen_by) > 1 or len(state.bands) > 1
    ident = state.canonical.flight or state.track_id
    flagstr = (" [" + ",".join(sorted(state.flags)) + "]") if state.flags else ""
    line = f"{state.track_id:<10s} {(ident or '').strip():<10s} band={bands:<9s} seen_by={seen}{flagstr}"
    return f"\033[1;32m{line}  <- MERGED\033[0m" if merged else line


@dataclass
class _Seen:
    """Tracks which flagged/drone/merged track_ids were announced last cycle,
    so each is announced only on transition (not every poll)."""

    flagged: set[str] = field(default_factory=set)
    drones: set[str] = field(default_factory=set)
    merged: set[str] = field(default_factory=set)


def _announce(merger: Merger, wanted: set[str], seen: _Seen) -> tuple[int, int]:
    """Print newly-appeared flagged aircraft, drones, and merge transitions.
    Returns (flagged_count, merged_count) for the heartbeat."""
    stamp = datetime.now(UTC).strftime("%H:%M:%S")
    states = merger.states.values()

    flagged = {s.track_id: s for s in states if s.flags & wanted}
    for tid in flagged.keys() - seen.flagged:
        print(f"[{stamp}] {_fmt(flagged[tid], wanted)}")
    seen.flagged = set(flagged)

    drones = {s.track_id: s for s in states if "remoteid" in s.bands}
    for tid in drones.keys() - seen.drones:
        print(f"[{stamp}] {_fmt_drone(drones[tid])}")
    seen.drones = set(drones)

    merged = {s.track_id: s for s in states if len(s.seen_by) > 1 or len(s.bands) > 1}
    for tid in merged.keys() - seen.merged:
        print(f"[{stamp}] MERGE  {_fmt_merged(merged[tid])}")
    for tid in seen.merged - merged.keys():
        print(f"[{stamp}] unmerge {tid}")
    seen.merged = set(merged)

    return len(flagged), len(merged)


async def watch(args: argparse.Namespace) -> int:
    """Poll all configured sources through the real multi-source pipeline
    (merge -> geometry -> DB enrich -> flags) and announce flagged aircraft +
    cross-source/band merges as they appear. Config is the source of truth;
    --url / --drone-url override sources, --only filters displayed flags."""
    config = _resolve_config(args.config)
    # --only filters which configured flags to announce; default = all of them.
    configured = set(config.enrichment.flags)
    wanted = {f.strip() for f in args.only.split(",")} if args.only else configured
    unknown = wanted - configured
    if unknown:
        print(
            f"--only names flags not in the config: {sorted(unknown)}; have: {sorted(configured)}"
        )
        return 2

    store = load_databases(Path(args.cache_dir), refresh=args.refresh)
    enricher = Enricher(config.enrichment, db_store=store)
    watchpoints = config.watchpoints_runtime()
    sources = _build_sources(config, args)
    merger = Merger()

    names = ", ".join(f"{s.name}({s.band})" for s in sources)
    print(f"Sources: {names}")
    print(f"Polling every {args.interval}s — Ctrl-C to stop")
    print(f"Flags: {', '.join(sorted(wanted)) or '(none configured)'}")
    print("(announces flagged aircraft, drones, + merges as they appear; heartbeat ~30s)\n")
    state_tracking = _Seen()
    last_beat = 0.0
    try:
        while True:
            for src in sources:
                for obs in await src.fetch():
                    state = merger.ingest(obs)
                    _apply_geometry(state, watchpoints)
                    enricher.enrich(state)
            n_flagged, n_merged = _announce(merger, wanted, state_tracking)

            now = asyncio.get_event_loop().time()
            if now - last_beat >= 30.0:
                stamp = datetime.now(UTC).strftime("%H:%M:%S")
                bands = _band_breakdown(merger)
                print(
                    f"[{stamp}] alive — {len(merger.states)} tracks ({bands}), "
                    f"{n_flagged} flagged, {n_merged} merged"
                )
                last_beat = now
            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        for src in sources:
            aclose = getattr(src, "aclose", None)
            if aclose is not None:
                await aclose()


def _band_breakdown(merger: Merger) -> str:
    """Compact per-band track count for the heartbeat, e.g. '1090:20 978:2'."""
    counts: dict[str, int] = {}
    for s in merger.states.values():
        for band in s.bands:
            counts[band] = counts.get(band, 0) + 1
    return " ".join(f"{b}:{counts[b]}" for b in sorted(counts)) or "none"


def _apply_geometry(state: AircraftState, watchpoints: list) -> None:  # type: ignore[type-arg]
    """Compute distance/bearing from each watchpoint (the tracker does this in
    the real service; the watcher replicates it so merged tracks have distance)."""
    c = state.canonical
    if c.lat is None or c.lon is None:
        state.distance_to.clear()
        state.bearing_to.clear()
        return
    for wp in watchpoints:
        state.distance_to[wp.name] = haversine(wp.lat, wp.lon, c.lat, c.lon)
        state.bearing_to[wp.name] = bearing(wp.lat, wp.lon, c.lat, c.lon)


def main() -> int:
    p = argparse.ArgumentParser(description="Live airspace watcher for ha-airspace.")
    p.add_argument(
        "--config",
        default="",
        help="config path. Default: auto-discover config.local.yaml / config.yaml.",
    )
    p.add_argument(
        "--url",
        default="",
        help="override: a single ad-hoc 1090 aircraft.json URL instead of config receivers.",
    )
    p.add_argument(
        "--drone-url",
        nargs="?",
        const=DEFAULT_DRONE_URL,
        default="",
        help=(
            "override the drone feed. Bare --drone-url uses the default "
            f"({DEFAULT_DRONE_URL}); or pass a URL."
        ),
    )
    p.add_argument(
        "--only",
        default="",
        help="comma-separated subset of the config's flags to announce (default: all).",
    )
    p.add_argument("--interval", type=float, default=2.0, help="poll seconds")
    p.add_argument("--cache-dir", default="/tmp/adsb-db-cache", help="DB cache directory")
    p.add_argument("--refresh", action="store_true", help="force re-download of the DBs")
    args = p.parse_args()
    return asyncio.run(watch(args))


if __name__ == "__main__":
    sys.exit(main())
