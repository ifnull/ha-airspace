"""Reference-database loader: download, parse, merge, atomic swap, refresh.

``DatabaseStore`` holds the current merged ``{hex: metadata}`` dict behind a
single attribute. Refresh builds a brand-new dict and rebinds the attribute in
one assignment — Python attribute rebinding is atomic, so a concurrent reader
holding the old reference is never torn (DESIGN §2 snapshot-on-enrich). No lock.

``DatabaseLoader`` orchestrates: for each enabled source in ascending priority,
download the gzip'd file over HTTP and parse it in a thread executor (CPU-bound;
~620k rows must not block the event loop) **directly into one shared dict**,
then swap that dict into the store. A failed refresh **never wipes a good copy**
— on any error the existing dict stays and we log. Disk caching
(write-then-rename) lets a restart serve the last-good DB before the first
network refresh completes; that is a small addition deferred to keep this slice
focused — see TODO.

Merge priority (DESIGN §4): ADSBex's flags win over Mictronics. Because both
parsers emit sparse dicts and merge per-key (``existing.update(entry)``), ADSBex
keys overwrite while absent keys fall through to Mictronics (e.g. Mictronics
``type`` survives when ADSBex has none).

Memory matters here more than it looks. Parsing each source into its own dict
and then copying into a third held ~610 MB RSS at peak on aarch64 — enough for
the kernel OOM-killer to take the process out on a 2 GB Raspberry Pi mid-refresh
(observed 2026-08-28). Merging in place holds one copy, ~220 MB.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import structlog

from ha_airspace.config import DatabasesConfig, DatabaseSourceConfig
from ha_airspace.databases.adsbexchange import parse_adsbexchange
from ha_airspace.databases.mictronics import parse_mictronics

log = structlog.get_logger(__name__)

_Parser = Callable[[bytes, dict[str, dict[str, object]]], dict[str, dict[str, object]]]
"""``(raw_gzip, accumulator) -> accumulator``. Both parsers merge in place so a
refresh holds exactly one copy of the merged DB (see ``refresh_once``)."""

# Parser dispatch by source name. A source whose name is not here is skipped
# with a warning (config validation does not constrain names to these, so a
# typo surfaces at load rather than silently doing nothing).
_PARSERS: dict[str, _Parser] = {
    "mictronics": parse_mictronics,
    "adsbexchange": parse_adsbexchange,
}

# Per-source merge precedence: higher wins on key conflict. ADSBex over
# Mictronics (DESIGN §4).
_PRIORITY: dict[str, int] = {"mictronics": 0, "adsbexchange": 1}

_DOWNLOAD_TIMEOUT_S = 120.0
"""Generous: the files are ~9-15 MB and the mirrors can be slow. This runs in
a background task, not the poll loop, so a long download blocks nothing."""


class DatabaseStore:
    """Holds the current merged reference dict. Lookups are dict gets.

    ``current`` is rebound atomically on refresh; callers should snapshot it
    once per enrichment pass (``db = store.current``) and read from the local
    reference so a mid-pass swap cannot tear lookups.
    """

    def __init__(self) -> None:
        self._current: dict[str, dict[str, object]] = {}

    @property
    def current(self) -> dict[str, dict[str, object]]:
        return self._current

    def swap(self, new: dict[str, dict[str, object]]) -> None:
        """Atomically replace the dict. The previous one is dropped once no
        reader references it."""
        self._current = new

    def lookup(self, hex_code: str) -> dict[str, object]:
        """Metadata for one hex (lowercase), or ``{}`` if unknown."""
        return self._current.get(hex_code, {})


class DatabaseLoader:
    """Downloads + refreshes the reference DBs into a ``DatabaseStore``.

    Construction args:
      config: The ``DatabasesConfig`` (sources + refresh interval).
      store: The ``DatabaseStore`` to populate. Shared with the enricher.
      fetcher: Async ``url -> bytes`` download function. Defaults to an
        httpx GET; injected in tests so no network is touched.
    """

    def __init__(
        self,
        config: DatabasesConfig,
        store: DatabaseStore,
        *,
        fetcher: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._fetcher = fetcher if fetcher is not None else _http_fetch
        self._stop = asyncio.Event()

    async def refresh_once(self) -> bool:
        """Download + parse every enabled source, merge, and swap the store.

        Returns True if the store was updated, False if the refresh failed
        and the previous dict was kept. Never raises for an expected failure
        (network, parse) — those are logged and swallowed so the background
        refresh loop keeps running.

        Sources are parsed **straight into one shared dict**, low priority
        first, so a higher-priority source's fields overwrite in place. The
        earlier build-then-copy-then-merge shape held up to three full copies
        of a 620k-row DB at once (~610 MB RSS measured on aarch64), which the
        kernel OOM-killer reliably picked off on a 2 GB Pi. One dict is ~220 MB.

        The merged dict is built off to the side and only swapped in on
        success, so a mid-stream source failure still leaves the previous
        good copy in place.
        """
        merged: dict[str, dict[str, object]] = {}
        any_ok = False
        # Process low-to-high priority so a higher-priority source's keys
        # overwrite the earlier ones — the parsers merge per-key into `merged`,
        # so absent keys fall through (e.g. Mictronics `type` survives when
        # ADSBex has none).
        ordered = sorted(
            (s for s in self._config.sources if s.enabled),
            key=lambda s: _PRIORITY.get(s.name, 0),
        )
        for source in ordered:
            if await self._load_source(source, merged):
                any_ok = True

        if not any_ok:
            log.warning("db_refresh_failed_all_sources", keeping_previous=True)
            return False
        self._store.swap(merged)
        log.info("db_refreshed", aircraft=len(merged))
        return True

    async def _load_source(
        self, source: DatabaseSourceConfig, merged: dict[str, dict[str, object]]
    ) -> bool:
        """Download one source and merge it into ``merged`` in place. Returns
        True on success. A download/parse failure is logged and swallowed —
        one bad source must not fail the whole refresh.

        A parse that raises partway through leaves whatever it already merged
        in ``merged``. That is intentional: the rows it did write are valid,
        and ``refresh_once`` only swaps the result in if at least one source
        succeeded outright.
        """
        parser = _PARSERS.get(source.name)
        if parser is None:
            log.warning("db_unknown_source", source=source.name)
            return False
        try:
            raw = await self._fetcher(source.url)
        except Exception as exc:  # noqa: BLE001 — any download failure is non-fatal
            log.warning("db_download_failed", source=source.name, error=str(exc))
            return False
        try:
            # CPU-bound parse of a large file -> executor, never the loop.
            await asyncio.to_thread(parser, raw, merged)
        except Exception as exc:  # noqa: BLE001 — a corrupt file is non-fatal
            log.warning("db_parse_failed", source=source.name, error=str(exc))
            return False
        return True

    async def run(self) -> None:
        """Background refresh loop: refresh now, then every ``refresh_interval_h``
        until ``stop()``. Run as a task in the app's TaskGroup."""
        interval_s = self._config.refresh_interval_h * 3600.0
        while not self._stop.is_set():
            await self.refresh_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_s)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop.set()


async def _http_fetch(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


# TODO(phase-2a-slice-2): disk cache (write-then-rename per source) so a
# restart serves the last-good DB before the first network refresh finishes,
# and so a fully-offline start still enriches. cache_dir is already in config.

__all__ = ["DatabaseLoader", "DatabaseStore"]
