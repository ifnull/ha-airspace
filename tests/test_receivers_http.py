"""Tests for HttpJsonReceiver.

Network is replaced with httpx.MockTransport everywhere — no actual
sockets open during the test suite (CLAUDE.md "no network in tests").
The transport handler is a callable that inspects the request URL and
returns the appropriate canned response, so the same fixture can serve
both ``aircraft.json`` and ``receiver.json`` from one HttpJsonReceiver.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ha_airspace.config import AuthConfig
from ha_airspace.receivers.http import (
    HttpJsonReceiver,
    _build_auth,
    _derive_receiver_json_url,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _aircraft_response_body() -> dict[str, Any]:
    return {
        "now": 1730000000.5,
        "messages": 12345678,
        "aircraft": [
            {
                "hex": "ae0001",
                "flight": "RCH171  ",
                "alt_baro": 35000,
                "lat": 30.33,
                "lon": -97.99,
                "gs": 480.5,
                "track": 90.0,
                "category": "A4",
                "nic": 8,
                "rssi": -12.3,
                "seen": 0.5,
                "seen_pos": 0.5,
            }
        ],
    }


def _make_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _make_receiver(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    name: str = "rx-home",
    band: str = "1090",
    url: str = "http://piaware.local/skyaware/data/aircraft.json",
    auth: AuthConfig | None = None,
    timeout_s: float = 5.0,
) -> HttpJsonReceiver:
    return HttpJsonReceiver(
        name=name,
        band=band,
        url=url,
        timeout_s=timeout_s,
        auth=auth,
        transport=_make_transport(handler),
    )


# ---------------------------------------------------------------------------
# Happy path: fetch()
# ---------------------------------------------------------------------------


class TestFetchSuccess:
    async def test_returns_observations_from_aircraft_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/aircraft.json")
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        observations = await rx.fetch()
        assert len(observations) == 1
        assert observations[0].hex == "ae0001"
        assert observations[0].flight == "RCH171"
        assert observations[0].band == "1090"
        assert observations[0].seen_by == "rx-home"
        await rx.aclose()

    async def test_health_marks_online_after_success(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        await rx.fetch()
        h = await rx.health()
        assert h["online"] is True
        assert h["aircraft_count"] == 1
        assert h["consecutive_failures"] == 0
        await rx.aclose()

    async def test_empty_aircraft_array_succeeds(self) -> None:
        # Empty airspace is a successful poll, not a failure.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"now": 1.0, "aircraft": []})

        rx = _make_receiver(handler)
        observations = await rx.fetch()
        assert observations == []
        h = await rx.health()
        assert h["consecutive_failures"] == 0
        await rx.aclose()


# ---------------------------------------------------------------------------
# Failure paths — every error class from the eng review error/rescue map.
# ---------------------------------------------------------------------------


class TestFetchFailures:
    async def test_timeout_returns_empty_and_increments_failures(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout", request=request)

        rx = _make_receiver(handler)
        result = await rx.fetch()
        assert result == []
        h = await rx.health()
        assert h["consecutive_failures"] == 1
        await rx.aclose()

    async def test_connect_error_returns_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated", request=request)

        rx = _make_receiver(handler)
        assert await rx.fetch() == []
        h = await rx.health()
        assert h["consecutive_failures"] == 1
        await rx.aclose()

    async def test_5xx_status_returns_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        rx = _make_receiver(handler)
        assert await rx.fetch() == []
        h = await rx.health()
        assert h["consecutive_failures"] == 1
        await rx.aclose()

    async def test_4xx_status_returns_empty(self) -> None:
        # 404 from a misconfigured URL should be a transient failure
        # (the user can fix the URL and the receiver recovers) — not
        # a hard crash.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        rx = _make_receiver(handler)
        assert await rx.fetch() == []
        await rx.aclose()

    async def test_malformed_json_returns_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"<html>not json</html>",
                headers={"content-type": "text/html"},
            )

        rx = _make_receiver(handler)
        assert await rx.fetch() == []
        h = await rx.health()
        assert h["consecutive_failures"] == 1
        await rx.aclose()

    async def test_schema_drift_returns_empty(self) -> None:
        # Valid JSON, missing 'aircraft' key.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"hello": "world"})

        rx = _make_receiver(handler)
        assert await rx.fetch() == []
        h = await rx.health()
        assert h["consecutive_failures"] == 1
        await rx.aclose()

    async def test_three_failures_marks_unhealthy(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated", request=request)

        rx = _make_receiver(handler)
        for _ in range(3):
            await rx.fetch()
        h = await rx.health()
        assert h["online"] is False
        assert h["consecutive_failures"] == 3
        await rx.aclose()

    async def test_recovery_resets_failure_counter(self) -> None:
        responses = iter(
            [
                # First two fail, then recover.
                "fail",
                "fail",
                "ok",
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            outcome = next(responses)
            if outcome == "fail":
                raise httpx.ConnectError("simulated", request=request)
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        await rx.fetch()
        await rx.fetch()
        assert (await rx.health())["consecutive_failures"] == 2
        await rx.fetch()
        h = await rx.health()
        assert h["consecutive_failures"] == 0
        assert h["online"] is True
        await rx.aclose()


# ---------------------------------------------------------------------------
# location() — receiver.json discovery
# ---------------------------------------------------------------------------


class TestLocation:
    async def test_returns_location_when_receiver_json_present(self) -> None:
        # Same handler serves both aircraft.json and receiver.json, the
        # path determines which canned response to return.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/receiver.json"):
                return httpx.Response(
                    200,
                    json={"version": "v8.2", "lat": 30.3322, "lon": -97.9853},
                )
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        loc = await rx.location()
        assert loc is not None
        assert loc.lat == 30.3322
        assert loc.lon == -97.9853
        assert loc.source == "receiver_json"
        await rx.aclose()

    async def test_returns_none_when_receiver_json_404(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/receiver.json"):
                return httpx.Response(404)
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        loc = await rx.location()
        assert loc is None
        await rx.aclose()

    async def test_returns_none_when_receiver_json_lacks_lat_lon(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/receiver.json"):
                # Some operators redact location for privacy.
                return httpx.Response(200, json={"version": "v8.2", "refresh": 1000})
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        loc = await rx.location()
        assert loc is None
        await rx.aclose()

    async def test_returns_none_when_receiver_json_has_string_lat(self) -> None:
        # Defensive against schema drift: lat/lon must be numeric.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/receiver.json"):
                return httpx.Response(200, json={"lat": "not a number", "lon": 0})
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        loc = await rx.location()
        assert loc is None
        await rx.aclose()

    async def test_returns_none_when_receiver_json_has_bool_lat(self) -> None:
        # bool subclasses int in Python — explicit reject preserves the
        # parser's safety pattern.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/receiver.json"):
                return httpx.Response(200, json={"lat": True, "lon": False})
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        loc = await rx.location()
        assert loc is None
        await rx.aclose()

    async def test_returns_none_when_url_does_not_end_in_aircraft_json(self) -> None:
        # Custom path layouts that do not follow the aircraft.json
        # convention return None; caller falls back to config-supplied
        # location.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler, url="http://piaware.local/custom-endpoint")
        assert await rx.location() is None
        await rx.aclose()

    async def test_returns_none_when_receiver_json_returns_non_dict(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/receiver.json"):
                return httpx.Response(200, json=[1, 2, 3])
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        assert await rx.location() is None
        await rx.aclose()

    async def test_returns_none_on_receiver_json_malformed_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/receiver.json"):
                return httpx.Response(
                    200,
                    content=b"<html>not json</html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        assert await rx.location() is None
        await rx.aclose()

    async def test_returns_none_when_receiver_json_times_out(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/receiver.json"):
                raise httpx.TimeoutException("slow", request=request)
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        # Should NOT raise; just return None and log.
        assert await rx.location() is None
        await rx.aclose()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    async def test_no_auth_sends_no_authorization_header(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        await rx.fetch()
        assert "authorization" not in {h.lower() for h in captured[0].headers}
        await rx.aclose()

    async def test_basic_auth_sends_authorization_header(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(
            handler,
            auth=AuthConfig(type="basic", username="u", password="p"),
        )
        await rx.fetch()
        # httpx writes Basic auth as Authorization: Basic <base64>
        auth_header = captured[0].headers.get("authorization")
        assert auth_header is not None
        assert auth_header.startswith("Basic ")
        await rx.aclose()

    async def test_header_auth_attaches_custom_headers(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(
            handler,
            auth=AuthConfig(
                type="header",
                headers={
                    "Authorization": "Bearer mytoken",
                    "X-Forwarded-User": "daniel",
                },
            ),
        )
        await rx.fetch()
        sent = captured[0].headers
        assert sent.get("authorization") == "Bearer mytoken"
        assert sent.get("x-forwarded-user") == "daniel"
        await rx.aclose()


class TestBuildAuthHelper:
    """Direct unit tests on the AuthConfig -> httpx primitives mapping."""

    def test_none_returns_no_auth_no_headers(self) -> None:
        client_auth, headers = _build_auth(None)
        assert client_auth is None
        assert headers == {}

    def test_type_none_returns_no_auth_no_headers(self) -> None:
        client_auth, headers = _build_auth(AuthConfig(type="none"))
        assert client_auth is None
        assert headers == {}

    def test_type_basic_returns_basic_auth(self) -> None:
        client_auth, headers = _build_auth(AuthConfig(type="basic", username="u", password="p"))
        assert isinstance(client_auth, httpx.BasicAuth)
        assert headers == {}

    def test_type_header_returns_headers_dict(self) -> None:
        client_auth, headers = _build_auth(
            AuthConfig(
                type="header",
                headers={"Authorization": "Bearer x"},
            )
        )
        assert client_auth is None
        assert headers == {"Authorization": "Bearer x"}


# ---------------------------------------------------------------------------
# URL derivation
# ---------------------------------------------------------------------------


class TestReceiverJsonUrlDerivation:
    def test_simple_path(self) -> None:
        assert (
            _derive_receiver_json_url("http://piaware/skyaware/data/aircraft.json")
            == "http://piaware/skyaware/data/receiver.json"
        )

    def test_dump1090_mutability_path(self) -> None:
        assert (
            _derive_receiver_json_url("http://pi:8080/dump1090/data/aircraft.json")
            == "http://pi:8080/dump1090/data/receiver.json"
        )

    def test_tar1090_path(self) -> None:
        assert (
            _derive_receiver_json_url("http://pi/tar1090/data/aircraft.json")
            == "http://pi/tar1090/data/receiver.json"
        )

    def test_no_aircraft_json_suffix_returns_none(self) -> None:
        assert _derive_receiver_json_url("http://pi/custom") is None
        assert _derive_receiver_json_url("http://pi/data/something.json") is None

    def test_aircraft_json_mid_path_does_not_match(self) -> None:
        # Hypothetical edge case: aircraft.json appears mid-path. We
        # only match the trailing segment.
        assert _derive_receiver_json_url("http://pi/aircraft.json/extra") is None

    def test_query_string_preserved(self) -> None:
        assert (
            _derive_receiver_json_url("http://pi/data/aircraft.json?nocache=1")
            == "http://pi/data/receiver.json?nocache=1"
        )

    def test_query_string_with_no_aircraft_json_returns_none(self) -> None:
        assert _derive_receiver_json_url("http://pi/data/foo?aircraft.json") is None


# ---------------------------------------------------------------------------
# Resource cleanup
# ---------------------------------------------------------------------------


class TestAclose:
    async def test_aclose_closes_underlying_client(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        await rx.fetch()
        await rx.aclose()
        # After close, fetch should fail (httpx raises on a closed
        # client). Verify the close actually took effect.
        with pytest.raises(RuntimeError):
            await rx.fetch()

    async def test_aclose_is_idempotent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        await rx.aclose()
        # Second close must not raise. httpx.AsyncClient.aclose() is
        # itself idempotent; we verify that here.
        await rx.aclose()


# ---------------------------------------------------------------------------
# Integration with provenance
# ---------------------------------------------------------------------------


class TestProvenance:
    async def test_observation_carries_receiver_name_and_band(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler, name="rx-airport", band="978")
        observations = await rx.fetch()
        assert observations[0].seen_by == "rx-airport"
        assert observations[0].band == "978"
        await rx.aclose()

    async def test_observed_at_is_utc_aware(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_aircraft_response_body())

        rx = _make_receiver(handler)
        observations = await rx.fetch()
        assert observations[0].observed_at.tzinfo is not None
        await rx.aclose()


# ---------------------------------------------------------------------------
# Sanity: the receiver still works against a fresh fixture, end-to-end.
# ---------------------------------------------------------------------------


class TestEndToEndWithRealFixture:
    async def test_serves_basic_fixture_via_mock_transport(self) -> None:
        # Read the captured fixture used by FileReceiver tests and serve
        # it over the mock transport. Cross-checks that HttpJsonReceiver
        # produces equivalent observations to FileReceiver from the same
        # bytes.
        fixture = (Path(__file__).parent / "fixtures" / "aircraft_basic.json").read_text(
            encoding="utf-8"
        )
        body = json.loads(fixture)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        rx = _make_receiver(handler)
        observations = await rx.fetch()
        assert len(observations) == 2
        hexes = {o.hex for o in observations}
        assert hexes == {"ae0001", "abc123"}
        await rx.aclose()
