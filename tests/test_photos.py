"""Tests for ha_airspace.photos.PhotoEnricher.

No network: an httpx.MockTransport stands in for Planespotters. Covers the
happy path, fails-soft on every error shape, and the TTL cache (hits + misses)
with an injected clock.
"""

from __future__ import annotations

import httpx
import pytest

from ha_airspace.photos import PhotoEnricher

_HEX = "4ca853"
_PHOTO_JSON = {
    "photos": [
        {
            "id": "abc",
            "thumbnail": {"src": "https://t.planespotters.net/img.jpg", "size": {"w": 200}},
            "thumbnail_large": {"src": "https://t.planespotters.net/large.jpg"},
            "link": "https://www.planespotters.net/photo/abc",
            "photographer": "Jane Doe",
        }
    ]
}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def _enricher(
    handler: object, *, cache_ttl_s: float = 60.0, clock: _Clock | None = None
) -> tuple[PhotoEnricher, _Clock]:
    clk = clock or _Clock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return PhotoEnricher(client, cache_ttl_s=cache_ttl_s, clock=clk), clk


class TestFetch:
    async def test_hit_returns_payload(self) -> None:
        enr, _ = _enricher(lambda req: httpx.Response(200, json=_PHOTO_JSON))
        photo = await enr.photo_for(_HEX)
        assert photo is not None
        assert photo.thumbnail_url == "https://t.planespotters.net/img.jpg"
        assert photo.link == "https://www.planespotters.net/photo/abc"
        assert photo.photographer == "Jane Doe"

    async def test_requests_correct_url(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            return httpx.Response(200, json=_PHOTO_JSON)

        enr, _ = _enricher(handler)
        await enr.photo_for(_HEX)
        assert seen == [f"https://api.planespotters.net/pub/photos/hex/{_HEX}"]

    async def test_no_photos_returns_none(self) -> None:
        enr, _ = _enricher(lambda req: httpx.Response(200, json={"photos": []}))
        assert await enr.photo_for(_HEX) is None

    async def test_missing_thumbnail_src_returns_none(self) -> None:
        enr, _ = _enricher(lambda req: httpx.Response(200, json={"photos": [{"link": "x"}]}))
        assert await enr.photo_for(_HEX) is None

    async def test_http_error_fails_soft(self) -> None:
        enr, _ = _enricher(lambda req: httpx.Response(500))
        assert await enr.photo_for(_HEX) is None

    async def test_timeout_fails_soft(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("slow", request=req)

        enr, _ = _enricher(handler)
        assert await enr.photo_for(_HEX) is None

    async def test_malformed_json_fails_soft(self) -> None:
        enr, _ = _enricher(lambda req: httpx.Response(200, content=b"not json"))
        assert await enr.photo_for(_HEX) is None


class TestCache:
    async def test_hit_is_cached(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=_PHOTO_JSON)

        enr, _ = _enricher(handler)
        await enr.photo_for(_HEX)
        await enr.photo_for(_HEX)
        assert calls["n"] == 1  # second call served from cache

    async def test_miss_is_cached(self) -> None:
        # A photoless hex must not be refetched on every alert.
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"photos": []})

        enr, _ = _enricher(handler)
        assert await enr.photo_for(_HEX) is None
        assert await enr.photo_for(_HEX) is None
        assert calls["n"] == 1

    async def test_ttl_expiry_refetches(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=_PHOTO_JSON)

        enr, clk = _enricher(handler, cache_ttl_s=60.0)
        await enr.photo_for(_HEX)
        clk.advance(61.0)  # past TTL
        await enr.photo_for(_HEX)
        assert calls["n"] == 2

    async def test_within_ttl_does_not_refetch(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=_PHOTO_JSON)

        enr, clk = _enricher(handler, cache_ttl_s=60.0)
        await enr.photo_for(_HEX)
        clk.advance(59.0)  # still within TTL
        await enr.photo_for(_HEX)
        assert calls["n"] == 1


@pytest.mark.parametrize("status", [200])
async def test_photo_for_never_raises(status: int) -> None:
    # Defensive: even a bizarre body shape yields None, not an exception.
    enr, _ = _enricher(lambda req: httpx.Response(status, json={"unexpected": True}))
    assert await enr.photo_for(_HEX) is None
