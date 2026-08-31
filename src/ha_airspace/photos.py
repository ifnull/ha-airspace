"""Planespotters aircraft-photo enrichment (Phase 2c).

Looks up a photo by ICAO hex and returns a ``PhotoPayload`` (thumbnail URL +
attribution) for injection into alert payloads. Deliberately small and
defensive:

* **Off unless configured** — the app only builds this when ``photos.enabled``.
* **Cached in memory** with a TTL (``photos.cache_ttl_days``). A *confirmed*
  absence is cached for the full TTL too, so a photoless hex is not refetched on
  every alert. No disk writes — the cache is a dict, SD-card-friendly.
* **A failed lookup is not an absence.** An answered "no photo for this hex" is
  good for weeks; a Cloudflare 525 or a read timeout says nothing about the
  airframe. Failures cache for ``FAILURE_CACHE_TTL_S`` instead — long enough not
  to hammer a struggling upstream on every alert, short enough that a blip does
  not blank an aircraft's photo for the next month.
* **Fails soft** — any timeout / HTTP error / malformed body logs a warning and
  yields ``None``. A photo lookup must never block or break an alert; the alert
  publishes without a photo and retries once the failure entry expires.
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

FAILURE_CACHE_TTL_S = 300.0
"""How long a *failed* lookup is remembered, versus ``cache_ttl_days`` for an
answered one. Five minutes: an upstream outage (observed: Cloudflare 525 from
api.planespotters.net) then costs at most one request per aircraft per five
minutes rather than one per alert, and recovers on its own without waiting out
the 30-day success TTL."""


class PhotoEnricher:
    """Hex -> aircraft photo, cached with a TTL and failing soft.

    Construction args:
      client: A long-lived ``httpx.AsyncClient`` (the app sets a descriptive
        User-Agent + timeout; tests inject a ``MockTransport``).
      cache_ttl_s: How long an *answered* lookup — a photo, or a confirmed
        absence — stays cached. Failures use ``failure_ttl_s``.
      failure_ttl_s: How long a failed lookup is remembered. Defaults to
        ``FAILURE_CACHE_TTL_S``.
      base_url: API base; defaults to Planespotters.
      clock: Monotonic clock for cache expiry; injectable for deterministic
        tests.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        cache_ttl_s: float,
        failure_ttl_s: float = FAILURE_CACHE_TTL_S,
        base_url: str = PLANESPOTTERS_BASE_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._cache_ttl_s = cache_ttl_s
        self._failure_ttl_s = failure_ttl_s
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        # hex -> (result, fetched_at_monotonic, ttl_s). result may be None for
        # both a confirmed absence and a failed lookup; the per-entry ttl is what
        # separates them, so the two never share an expiry.
        self._cache: dict[str, tuple[PhotoPayload | None, float, float]] = {}

    async def photo_for(self, hex_code: str) -> PhotoPayload | None:
        """Return the cached or freshly-fetched photo for ``hex_code``, or
        ``None`` if there is none / the lookup failed. Never raises."""
        cached = self._cache.get(hex_code)
        if cached is not None:
            result, fetched_at, ttl_s = cached
            if (self._clock() - fetched_at) < ttl_s:
                return result
        result, answered = await self._fetch(hex_code)
        ttl_s = self._cache_ttl_s if answered else self._failure_ttl_s
        self._cache[hex_code] = (result, self._clock(), ttl_s)
        return result

    async def _fetch(self, hex_code: str) -> tuple[PhotoPayload | None, bool]:
        """``(photo_or_none, answered)``. ``answered`` distinguishes "upstream
        told us there is no photo" from "we never got an answer" — only the
        former deserves the long cache TTL."""
        url = f"{self._base_url}/pub/photos/hex/{hex_code}"
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            photos = response.json().get("photos", [])
        except Exception as exc:  # noqa: BLE001 — fails soft: a photo is never worth an error
            # error_class matters: httpx's timeout/connect/protocol errors all
            # stringify to "" when raised without a message, which produced
            # `"error": ""` log lines saying nothing at all. Same shape as the
            # receiver failure log.
            log.warning(
                "photo_lookup_failed",
                hex=hex_code,
                error_class=type(exc).__name__,
                error=str(exc),
                retry_in_s=self._failure_ttl_s,
            )
            return None, False
        if not photos:
            return None, True
        first = photos[0]
        thumb = (first.get("thumbnail") or {}).get("src")
        if not thumb:
            return None, True
        return (
            PhotoPayload(
                thumbnail_url=thumb,
                link=first.get("link"),
                photographer=first.get("photographer"),
            ),
            True,
        )


__all__ = ["FAILURE_CACHE_TTL_S", "PLANESPOTTERS_BASE_URL", "PhotoEnricher"]
