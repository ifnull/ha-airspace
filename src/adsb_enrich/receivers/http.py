"""HttpJsonReceiver — the real receiver over HTTP.

Speaks dump1090-fa, dump1090-mutability, readsb, and dump978-fa: every
variant exposes the same ``aircraft.json`` shape, parsed by the shared
``parse_aircraft_json``. The receiver layer just owns the HTTP client
and translates wire failures into ``FetchError`` so the base class can
count them.

Lifecycle:

* ``__init__``: builds a long-lived ``httpx.AsyncClient`` configured
  with the per-receiver timeout, auth, and any custom headers. Per
  DESIGN.md §1, NEVER open a new client per request — the connection
  pool keeps polls fast on a Pi.
* ``fetch()``: handled by the base class; calls ``_do_fetch()`` here.
* ``location()``: derives the ``receiver.json`` URL by sibling-replacing
  the trailing ``aircraft.json``, fetches once on demand. Caller caches.
* ``aclose()``: closes the client. The merger calls this in its
  shutdown path; idempotent.

Tests inject an ``httpx.MockTransport`` to avoid real network I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from adsb_enrich.config import AuthConfig
from adsb_enrich.metrics import MetricsRegistry
from adsb_enrich.models import AircraftObservation, ReceiverLocation
from adsb_enrich.receivers._parse import parse_aircraft_json
from adsb_enrich.receivers.base import FetchError, ReceiverSource

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S: float = 5.0
"""Per-request timeout. Matches ``ServiceConfig.http_timeout_s`` default."""


class HttpJsonReceiver(ReceiverSource):
    """Polls a dump1090-style ``aircraft.json`` over HTTP.

    Args:
        name: Stable receiver identifier (used in MQTT topics + metric labels).
        band: ``"1090"`` or ``"978"``.
        url: Full URL to ``aircraft.json``, e.g.
            ``http://piaware.home.arpa:8080/skyaware/data/aircraft.json``.
        timeout_s: Per-request HTTP timeout.
        auth: Optional auth config; ``None`` means no auth.
        metrics: Optional ``MetricsRegistry`` (passed to the base class).
        transport: Optional ``httpx.AsyncBaseTransport`` override. Tests
            inject ``httpx.MockTransport`` to avoid real network calls;
            production leaves this ``None`` so httpx uses its default
            HTTP transport.
    """

    def __init__(
        self,
        name: str,
        band: str,
        url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        auth: AuthConfig | None = None,
        metrics: MetricsRegistry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(name, band, metrics=metrics)
        self._url = url

        client_auth, client_headers = _build_auth(auth)
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            auth=client_auth,
            headers=client_headers,
            transport=transport,
        )

    # ------------------------------------------------------------------
    # ReceiverSource hooks
    # ------------------------------------------------------------------

    async def _do_fetch(
        self,
    ) -> tuple[list[AircraftObservation], float | None]:
        """One HTTP GET + parse. Wraps every transient HTTP / decode /
        schema-shape failure in ``FetchError``."""
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise FetchError(f"receiver timeout: {self._url}") from exc
        except httpx.HTTPStatusError as exc:
            raise FetchError(f"receiver HTTP {exc.response.status_code}: {self._url}") from exc
        except httpx.HTTPError as exc:
            # Catches ConnectError, ReadError, RemoteProtocolError, etc.
            # — every transport-level failure not already specialized above.
            raise FetchError(f"receiver HTTP error: {self._url}: {exc}") from exc
        except ValueError as exc:
            # response.json() raises ValueError (json.JSONDecodeError is a
            # subclass) on malformed bodies.
            raise FetchError(f"malformed JSON from {self._url}: {exc}") from exc

        observed_at = datetime.now(UTC)
        try:
            return parse_aircraft_json(
                payload,
                receiver_name=self.name,
                band=self.band,
                observed_at=observed_at,
            )
        except ValueError as exc:
            raise FetchError(f"schema drift from {self._url}: {exc}") from exc

    async def location(self) -> ReceiverLocation | None:
        """Fetch the receiver's self-reported location from ``receiver.json``.

        Sibling discovery: replaces a trailing ``aircraft.json`` in the
        configured URL with ``receiver.json``. Returns ``None`` if:

        * The configured URL does not end in ``aircraft.json`` (custom
          path layout — caller falls back to config-supplied location).
        * The ``receiver.json`` request fails (404, timeout, etc.).
        * The response is missing ``lat`` or ``lon``.

        Per DESIGN.md the result is cached by the caller, not here, so
        every call hits the network. In practice the merger calls this
        once at startup.
        """
        receiver_url = _derive_receiver_json_url(self._url)
        if receiver_url is None:
            log.debug(
                "receiver_location_url_not_derivable",
                receiver=self.name,
                url=self._url,
            )
            return None

        try:
            response = await self._client.get(receiver_url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            log.info(
                "receiver_location_unavailable",
                receiver=self.name,
                url=receiver_url,
                error=str(exc),
            )
            return None
        except ValueError as exc:
            log.info(
                "receiver_location_malformed_json",
                receiver=self.name,
                url=receiver_url,
                error=str(exc),
            )
            return None

        if not isinstance(payload, dict):
            return None
        lat = payload.get("lat")
        lon = payload.get("lon")
        # Reject non-numeric AND bool-as-int (bool subclasses int).
        # Combined into one return to keep the function under the
        # too-many-returns budget without dropping any safety check.
        if (
            not isinstance(lat, int | float)
            or not isinstance(lon, int | float)
            or isinstance(lat, bool)
            or isinstance(lon, bool)
        ):
            return None

        return ReceiverLocation(lat=float(lat), lon=float(lon), source="receiver_json")

    # ------------------------------------------------------------------
    # Resource cleanup
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient``. Idempotent —
        ``httpx`` itself tracks closed-state internally. Called by the
        merger's shutdown path."""
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Auth conversion: AuthConfig -> (httpx auth, headers dict)
# ---------------------------------------------------------------------------


def _build_auth(
    auth: AuthConfig | None,
) -> tuple[httpx.Auth | None, dict[str, str]]:
    """Translate the validated ``AuthConfig`` into httpx primitives.

    Returns ``(client_auth, client_headers)`` ready to pass to
    ``httpx.AsyncClient(...)``. ``AuthConfig`` validation guarantees the
    required fields are populated for each ``type``, so we trust them
    here without re-checking.
    """
    if auth is None or auth.type == "none":
        return None, {}
    if auth.type == "basic":
        # AuthConfig validator guarantees username + password are set.
        assert auth.username is not None
        assert auth.password is not None
        return httpx.BasicAuth(auth.username, auth.password), {}
    # The Literal type narrows to "header" here; AuthConfig validator
    # guarantees headers is non-empty.
    return None, dict(auth.headers)


# ---------------------------------------------------------------------------
# URL derivation for receiver.json
# ---------------------------------------------------------------------------


def _derive_receiver_json_url(aircraft_url: str) -> str | None:
    """Replace a trailing ``aircraft.json`` in the URL path with
    ``receiver.json``. Returns ``None`` if the URL does not end in
    ``aircraft.json`` (caller treats as "no receiver.json available").
    """
    suffix = "aircraft.json"
    # Only replace when aircraft.json is the last path segment (before
    # any query string). Strict to avoid surprising substitutions in
    # paths with query parameters or fragments.
    if "?" in aircraft_url or "#" in aircraft_url:
        # Edge case: query string. Try to handle it without going full
        # urlparse — split off, re-attach.
        head, _, tail = aircraft_url.partition("?")
        if not head.endswith(suffix):
            return None
        return head.removesuffix(suffix) + "receiver.json" + ("?" + tail if tail else "")
    if not aircraft_url.endswith(suffix):
        return None
    return aircraft_url.removesuffix(suffix) + "receiver.json"
