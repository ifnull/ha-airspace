"""Application orchestration — wires the Phase 1 service together and runs it.

The composition root and run loop. ``build_app(config)`` constructs the real
collaborators from validated config; ``App.run()`` drives them:

  * one ``httpx``-backed ``HttpJsonReceiver`` per enabled receiver,
  * a long-lived ``MqttClient`` (reconnect, LWT, graceful shutdown),
  * a ``Publisher`` (topic routing, throttle, discovery),
  * a single ``AircraftTracker`` (lifecycle, geometry, nearest).

Phase 1 is single-source by design, but config allows a receiver *list*, so
the app runs one poll loop per receiver feeding one tracker. Concurrent polls
serialize on a lock so the tracker's state dict is never mutated mid-await.

# TODO(phase-3): the merger owns polling cadence and cross-receiver canonical
# selection. This per-receiver-loop + shared-tracker arrangement is the
# degenerate single-source case; replace the loops + AircraftTracker with the
# real merger when it lands.

Collaborators are injected into ``App`` so ``run()`` is testable end to end
with a ``FileReceiver`` and a fake MQTT client — no network, no broker. Only
``build_app`` touches the real network/broker constructors.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import TYPE_CHECKING

import structlog

from adsb_enrich import __version__
from adsb_enrich.config import Config
from adsb_enrich.enrichment import Enricher
from adsb_enrich.metrics import MetricsRegistry
from adsb_enrich.mqtt.client import MqttClient
from adsb_enrich.mqtt.publisher import Publisher
from adsb_enrich.receivers import HttpJsonReceiver, ReceiverSource
from adsb_enrich.tracker import AircraftTracker

if TYPE_CHECKING:
    from adsb_enrich.models import ReceiverLocation

log = structlog.get_logger(__name__)


class App:
    """Owns the run loop and the lifecycle of every collaborator.

    Construction args:
      config: Validated app config (for poll intervals + sw_version).
      receivers: One ``ReceiverSource`` per enabled receiver.
      mqtt_client: The connection-managing ``MqttClient``. Its
        ``on_connect`` is set here, overriding any earlier value.
      publisher: The topic-aware publish surface on top of the client.
      tracker: The single-source state tracker the poll loops feed.
      metrics: Optional ``MetricsRegistry`` (unused directly here; the
        collaborators hold their own references — kept for symmetry and
        future per-loop counters).
    """

    def __init__(
        self,
        config: Config,
        *,
        receivers: list[ReceiverSource],
        mqtt_client: MqttClient,
        publisher: Publisher,
        tracker: AircraftTracker,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._config = config
        self._receivers = receivers
        self._client = mqtt_client
        self._publisher = publisher
        self._tracker = tracker
        self._metrics = metrics

        # Receiver name -> cached self-reported location (fetched once at
        # startup, republished on every broker (re)connect).
        self._locations: dict[str, ReceiverLocation] = {}
        self._stop = asyncio.Event()
        self._tracker_lock = asyncio.Lock()

        # Resolve each receiver's poll interval once (receiver override else
        # service default), keyed by name. Unknown names fall back to the
        # service default — defensive, since names validate unique at load.
        self._intervals: dict[str, float] = {
            rc.name: config.poll_interval_for(rc) for rc in config.receivers
        }

        # The tracker and publisher must republish the discovery + status set
        # on every connect, so the client calls back into us.
        self._client.set_on_connect(self._on_broker_connect)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """Trigger graceful shutdown. Idempotent; safe from a signal
        handler. ``run()`` unwinds: drain + publish offline, then close
        receiver clients."""
        self._stop.set()

    async def run(self) -> None:
        """Main entry. Returns after a clean graceful shutdown.

        Caches receiver locations, installs signal handlers, then runs the
        MQTT client and one poll loop per receiver under a TaskGroup until
        ``request_stop()`` (or SIGTERM/SIGINT). On stop, the client drains
        and publishes offline; then receiver HTTP clients are closed.
        """
        self._install_signal_handlers()
        await self._cache_locations()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._client.run(), name="mqtt-client")
                for receiver in self._receivers:
                    tg.create_task(self._poll_loop(receiver), name=f"poll-{receiver.name}")
                tg.create_task(self._stop_watcher(), name="stop-watcher")
        finally:
            await self._close_receivers()
            log.info("service_stopped")

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def _cache_locations(self) -> None:
        """Fetch each receiver's self-reported location once. Failure is
        non-fatal — a receiver without a usable ``receiver.json`` simply
        has no location entity. Never blocks startup on a flaky receiver."""
        for receiver in self._receivers:
            try:
                location = await receiver.location()
            except Exception as exc:  # noqa: BLE001 — location is best-effort
                log.warning("receiver_location_failed", receiver=receiver.name, error=str(exc))
                continue
            if location is not None:
                self._locations[receiver.name] = location

    # ------------------------------------------------------------------
    # On-connect (runs on every successful broker connect + reconnect)
    # ------------------------------------------------------------------

    async def _on_broker_connect(self) -> None:
        """Republish the full retained set so a broker that lost its
        retained state (restart) is brought back to truth: service status
        + discovery, then each receiver's location and current status."""
        await self._publisher.on_connect(sw_version=__version__)
        for receiver in self._receivers:
            location = self._locations.get(receiver.name)
            if location is not None:
                await self._publisher.publish_receiver_location(receiver.name, location)
            health = await receiver.health()
            await self._publisher.publish_receiver_status(
                receiver.name, online=bool(health["online"])
            )

    # ------------------------------------------------------------------
    # Poll loop (one task per receiver)
    # ------------------------------------------------------------------

    async def _poll_loop(self, receiver: ReceiverSource) -> None:
        """Poll one receiver on its interval until stop. First poll is
        immediate; subsequent polls wait the interval (interruptible by
        ``request_stop`` so shutdown is prompt)."""
        interval = self._intervals.get(receiver.name, self._config.service.poll_interval_s)
        while not self._stop.is_set():
            await self._poll_once(receiver)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    async def _poll_once(self, receiver: ReceiverSource) -> None:
        """One fetch → tracker → receiver-status publish cycle. The base
        receiver swallows transient failures (returns ``[]`` and marks
        itself unhealthy), so this never crashes the loop on a flaky feed."""
        observations = await receiver.fetch()
        async with self._tracker_lock:
            await self._tracker.process_poll(observations)
        health = await receiver.health()
        await self._publisher.publish_receiver_status(receiver.name, online=bool(health["online"]))
        await self._publisher.publish_receiver_stats(receiver.name, health)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _stop_watcher(self) -> None:
        """Await the stop signal, then tell the client to shut down. The
        client's ``run()`` drains the queue, publishes offline retained,
        and exits; the poll loops see the stop event and exit on their own.
        Once every task returns, the TaskGroup completes."""
        await self._stop.wait()
        log.info("service_stopping")
        await self._client.stop()

    async def _close_receivers(self) -> None:
        for receiver in self._receivers:
            aclose = getattr(receiver, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Wire SIGTERM/SIGINT to ``request_stop`` on the running loop.

        Best-effort: ``add_signal_handler`` is unavailable on Windows and
        outside the main thread (e.g. some test runners). In that case the
        caller drives shutdown via ``request_stop`` directly."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self.request_stop)


def build_app(config: Config, *, metrics: MetricsRegistry | None = None) -> App:
    """Composition root: build the real collaborators from validated config.

    Constructs an ``HttpJsonReceiver`` per *enabled* receiver, the MQTT
    client + publisher, and a single tracker over the configured
    watchpoints. The only place that touches the real network/broker
    constructors — kept separate so ``App`` stays unit-testable with fakes.
    """
    metrics = metrics if metrics is not None else MetricsRegistry()

    receivers: list[ReceiverSource] = [
        HttpJsonReceiver(
            rc.name,
            rc.band,
            rc.url,
            timeout_s=config.service.http_timeout_s,
            auth=rc.auth,
            metrics=metrics,
        )
        for rc in config.receivers
        if rc.enabled
    ]
    if not receivers:
        raise ValueError("no enabled receivers in config")

    client = MqttClient(config.mqtt, metrics=metrics)
    publisher = Publisher(client, config, metrics=metrics)
    tracker = AircraftTracker(
        publisher,
        config.watchpoints_runtime(),
        enricher=Enricher(config.enrichment),
        metrics=metrics,
    )
    return App(
        config,
        receivers=receivers,
        mqtt_client=client,
        publisher=publisher,
        tracker=tracker,
        metrics=metrics,
    )


__all__ = ["App", "build_app"]
