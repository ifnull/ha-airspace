"""Planespotters aircraft-photo enrichment (Phase 2c).

Looks up a photo by ICAO hex and returns a ``PhotoPayload`` (thumbnail URL +
attribution) for injection into alert payloads. Deliberately small and
defensive:

* **Off unless configured** — the app only builds this when ``photos.enabled``.
* **Cached in memory** with a TTL (``photos.cache_ttl_days``). Misses are cached
  too, so a photoless hex is not refetched on every alert. No disk writes — the
  cache is a dict, SD-card-friendly.
* **Fails soft** — any timeout / HTTP error / malformed body logs a warning and
  yields ``None``. A photo lookup must never block or break an alert; the alert
  publishes without a photo and tries again after the cache entry expires.
* **No network in tests** — the ``httpx.AsyncClient`` is injected, so tests pass
  a ``MockTransport``.

Source: ``GET {base}/pub/photos/hex/{hex}`` ->
``{"photos": [{"thumbnail": {"src": ...}, "link": ..., "photographer": ...}]}``.
No API key. ``{"photos": []}`` when none.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from ha_airspace.mqtt.payloads import PhotoPayload

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

log = structlog.get_logger(__name__)

PLANESPOTTERS_BASE_URL = "https://api.planespotters.net"
"""Default Planespotters public API base. Overridable for tests / mirrors."""


class PhotoEnricher:
    """Hex -> aircraft photo, cached with a TTL and failing soft.

    Construction args:
      client: A long-lived ``httpx.AsyncClient`` (the app sets a descriptive
        User-Agent + timeout; tests inject a ``MockTransport``).
      cache_ttl_s: How long a result — hit *or* miss — stays cached.
      base_url: API base; defaults to Planespotters.
      clock: Monotonic clock for cache expiry; injectable for deterministic
        tests.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        cache_ttl_s: float,
        base_url: str = PLANESPOTTERS_BASE_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._cache_ttl_s = cache_ttl_s
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        # hex -> (result, fetched_at_monotonic). result may be None (cached miss).
        self._cache: dict[str, tuple[PhotoPayload | None, float]] = {}

    async def photo_for(self, hex_code: str) -> PhotoPayload | None:
        """Return the cached or freshly-fetched photo for ``hex_code``, or
        ``None`` if there is none / the lookup failed. Never raises."""
        cached = self._cache.get(hex_code)
        if cached is not None and (self._clock() - cached[1]) < self._cache_ttl_s:
            return cached[0]
        result = await self._fetch(hex_code)
        self._cache[hex_code] = (result, self._clock())
        return result

    async def _fetch(self, hex_code: str) -> PhotoPayload | None:
        url = f"{self._base_url}/pub/photos/hex/{hex_code}"
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            photos = response.json().get("photos", [])
        except Exception as exc:  # noqa: BLE001 — fails soft: a photo is never worth an error
            log.warning("photo_lookup_failed", hex=hex_code, error=str(exc))
            return None
        if not photos:
            return None
        first = photos[0]
        thumb = (first.get("thumbnail") or {}).get("src")
        if not thumb:
            return None
        return PhotoPayload(
            thumbnail_url=thumb,
            link=first.get("link"),
            photographer=first.get("photographer"),
        )


__all__ = ["PLANESPOTTERS_BASE_URL", "PhotoEnricher"]
