"""Tests for ha_airspace.photos.PhotoEnricher.

No network: an httpx.MockTransport stands in for Planespotters. Covers the
happy path, fails-soft on every error shape, and the TTL cache (hits + misses)
with an injected clock.
"""

from __future__ import annotations

import httpx
import pytest
import structlog

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
    handler: object,
    *,
    cache_ttl_s: float = 60.0,
    failure_ttl_s: float = 10.0,
    clock: _Clock | None = None,
) -> tuple[PhotoEnricher, _Clock]:
    clk = clock or _Clock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return (
        PhotoEnricher(client, cache_ttl_s=cache_ttl_s, failure_ttl_s=failure_ttl_s, clock=clk),
        clk,
    )


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


class TestFailureIsNotAnAbsence:
    """A 525 from Cloudflare says nothing about whether the airframe has a
    photo. Caching that for `cache_ttl_days` (30 by default) blanked an
    aircraft's photo for a month over a transient upstream blip — observed in
    the field 2026-08-31 against api.planespotters.net."""

    async def test_failed_lookup_retries_after_short_ttl(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            # Fail once (Cloudflare 525), then succeed.
            if len(calls) == 1:
                return httpx.Response(525)
            return httpx.Response(200, json=_PHOTO_JSON)

        enricher, clk = _enricher(handler, cache_ttl_s=60.0, failure_ttl_s=10.0)
        assert await enricher.photo_for(_HEX) is None
        # Within the failure TTL: served from cache, no second request.
        clk.advance(5.0)
        assert await enricher.photo_for(_HEX) is None
        assert len(calls) == 1
        # Past it: refetched, and the photo appears.
        clk.advance(6.0)
        result = await enricher.photo_for(_HEX)
        assert result is not None
        assert len(calls) == 2

    async def test_failure_does_not_get_the_success_ttl(self) -> None:
        """The regression itself: a failure must not be remembered as long as
        an answered lookup."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(525)

        enricher, _ = _enricher(handler, cache_ttl_s=2_592_000.0, failure_ttl_s=10.0)
        await enricher.photo_for(_HEX)
        # The 30-day success TTL must not be what a failed lookup gets.
        _, _, ttl = enricher._cache[_HEX]
        assert ttl == 10.0

    async def test_confirmed_absence_keeps_the_long_ttl(self) -> None:
        """An answered "no photos for this hex" is still good for weeks — that
        caching must not regress while fixing the failure case."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"photos": []})

        enricher, clk = _enricher(handler, cache_ttl_s=60.0, failure_ttl_s=10.0)
        assert await enricher.photo_for(_HEX) is None
        clk.advance(30.0)  # past the failure TTL, inside the success TTL
        assert await enricher.photo_for(_HEX) is None
        assert len(calls) == 1, "a confirmed absence was refetched too early"

    async def test_missing_thumbnail_counts_as_answered(self) -> None:
        """A 200 with a photo entry but no thumbnail src is a real answer, not
        a failure — upstream told us there is nothing usable."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"photos": [{"link": "https://x/1"}]})

        enricher, _ = _enricher(handler, cache_ttl_s=60.0, failure_ttl_s=10.0)
        await enricher.photo_for(_HEX)
        _, _, ttl = enricher._cache[_HEX]
        assert ttl == 60.0


class TestFailureLogging:
    async def test_logs_error_class_when_the_exception_stringifies_empty(self) -> None:
        """httpx's timeout/connect/protocol errors all `str()` to "" when raised
        without a message, which produced `"error": ""` log lines carrying no
        information at all."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("")

        enricher, _ = _enricher(handler)
        with structlog.testing.capture_logs() as logs:
            assert await enricher.photo_for(_HEX) is None
        failures = [entry for entry in logs if entry["event"] == "photo_lookup_failed"]
        assert len(failures) == 1
        assert failures[0]["error_class"] == "ReadTimeout"
        assert failures[0]["retry_in_s"] == 10.0
