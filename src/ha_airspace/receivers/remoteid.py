"""RemoteIdHttpReceiver — drone Remote ID over HTTP (dump3411 / FEED.md).

Polls a ``remoteid.json`` feed (the contract in ``FEED.md``, served by
``dump3411``) and maps detections to ``band="remoteid"`` observations via
``parse_remoteid_json``. Architecturally identical to ``HttpJsonReceiver`` —
long-lived ``httpx.AsyncClient``, ``FetchError`` on transient failure, the
base class counts failures — but with the Remote-ID specifics:

* ``band`` is fixed to ``"remoteid"``; the constructor takes no band.
* No auth and no ``receiver.json`` sibling: a Remote ID detector is a LAN
  appliance with a single JSON endpoint, so ``location()`` returns ``None``
  (the detector's own position is not part of the feed).

Tests inject an ``httpx.MockTransport`` to avoid real network I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from ha_airspace.metrics import MetricsRegistry
from ha_airspace.models import AircraftObservation, ReceiverLocation
from ha_airspace.receivers._parse import MessageRateTracker
from ha_airspace.receivers._remoteid_parse import parse_remoteid_json
from ha_airspace.receivers.base import FetchError, ReceiverSource

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S: float = 5.0
BAND: str = "remoteid"


class RemoteIdHttpReceiver(ReceiverSource):
    """Polls a ``remoteid.json`` Remote ID feed over HTTP.

    Args:
        name: Stable receiver identifier (MQTT topics + metric labels).
        url: Full URL to ``remoteid.json``, e.g.
            ``http://dump3411.local:8754/data/remoteid.json``.
        timeout_s: Per-request HTTP timeout.
        metrics: Optional ``MetricsRegistry`` (passed to the base class).
        transport: Optional ``httpx.AsyncBaseTransport`` override for tests.
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        metrics: MetricsRegistry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(name, BAND, metrics=metrics)
        self._url = url
        self._rate_tracker = MessageRateTracker()
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    async def _do_fetch(
        self,
    ) -> tuple[list[AircraftObservation], float | None]:
        """One HTTP GET + parse. Wraps every transient failure in ``FetchError``."""
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise FetchError(f"remoteid timeout: {self._url}") from exc
        except httpx.HTTPStatusError as exc:
            raise FetchError(f"remoteid HTTP {exc.response.status_code}: {self._url}") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"remoteid HTTP error: {self._url}: {exc}") from exc
        except ValueError as exc:
            raise FetchError(f"malformed JSON from {self._url}: {exc}") from exc

        observed_at = datetime.now(UTC)
        try:
            return parse_remoteid_json(
                payload,
                receiver_name=self.name,
                observed_at=observed_at,
                rate_tracker=self._rate_tracker,
            )
        except ValueError as exc:
            raise FetchError(f"schema drift from {self._url}: {exc}") from exc

    async def location(self) -> ReceiverLocation | None:
        """Remote ID feeds carry no detector location — return ``None``."""
        return None

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient``. Idempotent."""
        await self._client.aclose()


__all__ = ["RemoteIdHttpReceiver"]
