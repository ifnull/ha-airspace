"""Tests for ha_airspace.drone_registry.DroneRegistry.

No network: an httpx.MockTransport stands in for the FAA UAS DOC API. Covers the
happy path, the field mapping, fails-soft on every error shape, and the TTL
cache (hits + misses) with an injected clock.
"""

from __future__ import annotations

import httpx

from ha_airspace.drone_registry import DroneRegistry

_SERIAL = "1581F5BK000000000001"
_HIT = {
    "data": {
        "items": [
            {
                "makeName": "DJI",
                "modelName": "Mavic 3",
                "status": "accepted",
                "trackingNumber": "RID000000123",
                "docType": "rid",
                "updatedAt": "2026-01-01",
            }
        ]
    }
}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def _registry(
    handler: object, *, cache_ttl_s: float = 60.0, clock: _Clock | None = None
) -> tuple[DroneRegistry, _Clock]:
    clk = clock or _Clock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return DroneRegistry(client, cache_ttl_s=cache_ttl_s, clock=clk), clk


class TestLookup:
    async def test_hit_maps_fields(self) -> None:
        reg, _ = _registry(lambda req: httpx.Response(200, json=_HIT))
        info = await reg.lookup(_SERIAL)
        assert info == {
            "make": "DJI",
            "model": "Mavic 3",
            "status": "accepted",
            "rid_tracking": "RID000000123",
        }

    async def test_query_targets_serial_endpoint(self) -> None:
        seen: list[httpx.URL] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url)
            return httpx.Response(200, json=_HIT)

        reg, _ = _registry(handler)
        await reg.lookup(_SERIAL)
        assert seen[0].path == "/api/v1/serialNumbers"
        assert seen[0].params["serialNumber"] == _SERIAL
        assert seen[0].params["findBy"] == "serialNumber"

    async def test_no_items_returns_none(self) -> None:
        reg, _ = _registry(lambda req: httpx.Response(200, json={"data": {"items": []}}))
        assert await reg.lookup(_SERIAL) is None

    async def test_make_and_model_absent_returns_none(self) -> None:
        body = {"data": {"items": [{"status": "pending"}]}}
        reg, _ = _registry(lambda req: httpx.Response(200, json=body))
        assert await reg.lookup(_SERIAL) is None

    async def test_http_error_fails_soft(self) -> None:
        reg, _ = _registry(lambda req: httpx.Response(500))
        assert await reg.lookup(_SERIAL) is None

    async def test_timeout_fails_soft(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("slow", request=req)

        reg, _ = _registry(handler)
        assert await reg.lookup(_SERIAL) is None

    async def test_malformed_json_fails_soft(self) -> None:
        reg, _ = _registry(lambda req: httpx.Response(200, content=b"not json"))
        assert await reg.lookup(_SERIAL) is None


class TestCache:
    async def test_hit_is_cached(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=_HIT)

        reg, _ = _registry(handler)
        await reg.lookup(_SERIAL)
        await reg.lookup(_SERIAL)
        assert calls["n"] == 1

    async def test_miss_is_cached(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"data": {"items": []}})

        reg, _ = _registry(handler)
        assert await reg.lookup(_SERIAL) is None
        assert await reg.lookup(_SERIAL) is None
        assert calls["n"] == 1

    async def test_ttl_expiry_refetches(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=_HIT)

        reg, clk = _registry(handler, cache_ttl_s=60.0)
        await reg.lookup(_SERIAL)
        clk.advance(61.0)
        await reg.lookup(_SERIAL)
        assert calls["n"] == 2
