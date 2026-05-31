"""High-level publisher: topic routing + throttle + retention policy.

Sits on top of ``MqttClient``. Knows the Phase 1 topic taxonomy
(aircraft / summary / receiver / status / discovery), the retention
policy (state-bearing topics retained, transient signals not), and
the throttle policy (per-hex aircraft, global summary). The merger
calls these methods; the publisher translates calls into ``client.publish``
with the right topic, payload, retain, and topic_class.

Throttling uses ``time.monotonic`` by default (immune to wall-clock
adjustments) and is injectable via the ``clock`` arg for deterministic
tests.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import structlog

from adsb_enrich.config import Config
from adsb_enrich.metrics import MetricsRegistry
from adsb_enrich.models import AircraftState, ReceiverLocation
from adsb_enrich.mqtt.client import MqttClient
from adsb_enrich.mqtt.discovery import build_discovery_payloads
from adsb_enrich.mqtt.payloads import (
    AircraftPayload,
    ReceiverLocationPayload,
    ReceiverStatsPayload,
)

log = structlog.get_logger(__name__)


class Publisher:
    """Topic-aware MQTT publish surface.

    Construction args:
      client: An MqttClient — call ``publish_aircraft(...)``, etc., and
        the calls go through its publish queue.
      config: Validated app config. Provides the base_topic, the
        throttle intervals, and the discovery prefix.
      metrics: Optional MetricsRegistry, kept for symmetry with other
        modules; the actual publish counters are owned by MqttClient.
      clock: Override ``time.monotonic`` for deterministic throttle
        tests. Returns float seconds.
    """

    def __init__(
        self,
        client: MqttClient,
        config: Config,
        *,
        metrics: MetricsRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._config = config
        self._metrics = metrics
        self._clock = clock

        self._base = config.mqtt.base_topic
        self._aircraft_min_interval = config.mqtt.publish_aircraft_min_interval_s
        self._summary_min_interval = config.mqtt.publish_summary_min_interval_s

        # Throttle state. 0.0 sentinel always allows the first publish
        # since (now - 0.0) > any finite min_interval.
        self._last_aircraft_publish: dict[str, float] = {}
        self._last_summary_publish: float = 0.0

    # ------------------------------------------------------------------
    # On-connect hook (status:online + discovery republish)
    # ------------------------------------------------------------------

    async def on_connect(self, *, sw_version: str | None = None) -> None:
        """Run after every successful broker connect.

        Order matters:
          1. Publish ``adsb/status: online`` retained, so HA flips the
             availability sensors before discovery payloads land.
          2. Publish the full discovery payload set (idempotent on
             retained topics; protects against the broker losing
             retained state on a restart).

        The merger wires this into ``MqttClient.on_connect`` at startup.
        """
        await self._client.publish(
            f"{self._base}/status",
            b"online",
            retain=True,
            topic_class="status",
        )
        for topic, body in build_discovery_payloads(self._config, sw_version=sw_version):
            await self._client.publish(
                topic,
                json.dumps(body, separators=(",", ":")).encode("utf-8"),
                retain=True,
                topic_class="discovery",
            )

    # ------------------------------------------------------------------
    # Per-aircraft topic — power-user wildcard, NOT auto-discovered
    # ------------------------------------------------------------------

    async def publish_aircraft(self, state: AircraftState) -> bool:
        """Publish per-aircraft state to ``adsb/aircraft/<hex>``.

        Throttled per-hex by ``mqtt.publish_aircraft_min_interval_s``.
        Returns True if published, False if suppressed by throttle —
        the caller can use this for logging / metrics if it cares.
        """
        now = self._clock()
        last = self._last_aircraft_publish.get(state.hex, 0.0)
        if (now - last) < self._aircraft_min_interval:
            return False
        self._last_aircraft_publish[state.hex] = now

        payload = AircraftPayload.from_state(state).model_dump_json()
        await self._client.publish(
            f"{self._base}/aircraft/{state.hex}",
            payload,
            retain=True,
            topic_class="aircraft",
        )
        return True

    async def purge_aircraft(self, hex_code: str) -> None:
        """Clear the retained aircraft topic when state is PURGED.

        Empty payload + retain=True tells the broker to drop the
        retained value. Forgetting this leaves zombie aircraft in HA
        forever (CLAUDE.md "things that will trip you up").
        """
        await self._client.publish(
            f"{self._base}/aircraft/{hex_code}",
            b"",
            retain=True,
            topic_class="aircraft",
        )
        self._last_aircraft_publish.pop(hex_code, None)

    # ------------------------------------------------------------------
    # Summary topics — the surface HA actually subscribes to
    # ------------------------------------------------------------------

    async def publish_summary(
        self,
        *,
        count: int,
        nearest: AircraftState | None,
        count_by_flag: dict[str, int] | None = None,
    ) -> bool:
        """Publish ``adsb/summary/{count, nearest, count_by_flag}``.

        Globally throttled (one summary publish per ``mqtt.publish_summary_min_interval_s``).
        Returns True if published, False if throttled.

        ``nearest=None`` publishes empty-retained on the nearest topic
        so HA's Nearest Aircraft sensor goes unavailable rather than
        showing stale data when the airspace empties.
        """
        now = self._clock()
        if (now - self._last_summary_publish) < self._summary_min_interval:
            return False
        self._last_summary_publish = now

        await self._client.publish(
            f"{self._base}/summary/count",
            str(count).encode("utf-8"),
            retain=True,
            topic_class="summary",
        )

        nearest_payload: bytes | str
        if nearest is not None:
            nearest_payload = AircraftPayload.from_state(nearest).model_dump_json()
        else:
            nearest_payload = b""
        await self._client.publish(
            f"{self._base}/summary/nearest",
            nearest_payload,
            retain=True,
            topic_class="summary",
        )

        flags = count_by_flag if count_by_flag is not None else {}
        await self._client.publish(
            f"{self._base}/summary/count_by_flag",
            json.dumps(flags, separators=(",", ":")).encode("utf-8"),
            retain=True,
            topic_class="summary",
        )
        return True

    # ------------------------------------------------------------------
    # Per-receiver topics
    # ------------------------------------------------------------------

    async def publish_receiver_status(
        self, name: str, *, online: bool, unhealthy: bool = False
    ) -> None:
        """Publish ``adsb/receiver/<name>/status``: ``online`` |
        ``unhealthy`` | ``offline``. Retained — HA's connectivity
        binary_sensor reads from this directly via ``payload_on=online``."""
        if not online:
            status = "offline"
        elif unhealthy:
            status = "unhealthy"
        else:
            status = "online"
        await self._client.publish(
            f"{self._base}/receiver/{name}/status",
            status.encode("utf-8"),
            retain=True,
            topic_class="status",
        )

    async def publish_receiver_stats(self, name: str, health: dict[str, object]) -> None:
        """Publish ``adsb/receiver/<name>/stats`` as JSON. HA's
        per-receiver count and message-rate sensors extract via
        ``value_template`` from this single topic."""
        payload = ReceiverStatsPayload.from_health(health).model_dump_json()
        await self._client.publish(
            f"{self._base}/receiver/{name}/stats",
            payload,
            retain=True,
            topic_class="status",
        )

    async def publish_receiver_location(self, name: str, location: ReceiverLocation) -> None:
        """Publish ``adsb/receiver/<name>/location`` as JSON. Published
        once at startup (and republished on reconnect via on_connect)."""
        payload = ReceiverLocationPayload.from_runtime(location).model_dump_json()
        await self._client.publish(
            f"{self._base}/receiver/{name}/location",
            payload,
            retain=True,
            topic_class="status",
        )


__all__ = ["Publisher"]
