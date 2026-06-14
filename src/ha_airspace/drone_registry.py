"""FAA UAS make/model enrichment for drones (Phase 5+).

Looks up a drone's broadcast serial against the FAA UAS Declaration-of-Compliance
registry and returns make / model / status, to attach to the drone payload.
Small and defensive, mirroring ``photos.PhotoEnricher``:

* **Off unless configured** — the app only builds this when
  ``drone_registry.enabled``.
* **Live per-serial lookup, cached** with a TTL. Drones are infrequent, so this
  is a handful of lookups, not a bulk database; misses are cached too so an
  unknown serial isn't re-queried. In-memory only (no disk writes).
* **Fails soft** — any timeout / HTTP / parse error logs a warning and yields
  ``None``; enrichment never blocks or breaks a drone publish.
* **No network in tests** — the ``httpx.AsyncClient`` is injected.

Source: ``GET {base}/api/v1/serialNumbers?findBy=serialNumber&serialNumber=...``
-> ``{"data": {"items": [{"makeName", "modelName", "status", "trackingNumber",
"docType", ...}]}}``. No API key. The first item (most recently updated) wins.

This is compliance/product data, NOT operator identity — the FAA does not expose
owner/registrant publicly. Operator *location* comes from the Remote ID
broadcast (``DroneInfo.operator_lat/lon``), never from here.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

log = structlog.get_logger(__name__)

FAA_UASDOC_BASE_URL = "https://uasdoc.faa.gov"
"""Default FAA UAS Declaration-of-Compliance API base. Overridable for tests."""


class DroneRegistry:
    """Serial -> FAA make/model/status, cached with a TTL and failing soft.

    Construction args:
      client: A long-lived ``httpx.AsyncClient`` (the app sets a descriptive
        User-Agent + timeout; tests inject a ``MockTransport``).
      cache_ttl_s: How long a result — hit *or* miss — stays cached.
      base_url: API base; defaults to the FAA UAS DOC system.
      clock: Monotonic clock for cache expiry; injectable for tests.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        cache_ttl_s: float,
        base_url: str = FAA_UASDOC_BASE_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._cache_ttl_s = cache_ttl_s
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        # serial -> (result, fetched_at_monotonic). result may be None (miss).
        self._cache: dict[str, tuple[dict[str, Any] | None, float]] = {}

    async def lookup(self, serial: str) -> dict[str, Any] | None:
        """Return ``{make, model, status, rid_tracking}`` for ``serial``, or
        ``None`` if not found / the lookup failed. Cached; never raises."""
        cached = self._cache.get(serial)
        if cached is not None and (self._clock() - cached[1]) < self._cache_ttl_s:
            return cached[0]
        result = await self._fetch(serial)
        self._cache[serial] = (result, self._clock())
        return result

    async def _fetch(self, serial: str) -> dict[str, Any] | None:
        url = f"{self._base_url}/api/v1/serialNumbers"
        try:
            response = await self._client.get(
                url,
                params={
                    "findBy": "serialNumber",
                    "serialNumber": serial,
                    "orderBy[0]": "updatedAt",
                    "orderBy[1]": "DESC",
                },
                headers={"Accept": "application/json", "client": "external"},
            )
            response.raise_for_status()
            items = response.json().get("data", {}).get("items", [])
        except Exception as exc:  # noqa: BLE001 — fails soft: enrichment is never worth an error
            log.warning("drone_registry_lookup_failed", serial=serial, error=str(exc))
            return None
        if not items:
            return None
        item = items[0]  # most recently updated
        make = item.get("makeName")
        model = item.get("modelName")
        if not make and not model:
            return None  # nothing useful to attach
        return {
            "make": make,
            "model": model,
            "status": item.get("status"),
            "rid_tracking": item.get("trackingNumber"),
        }


__all__ = ["FAA_UASDOC_BASE_URL", "DroneRegistry"]
