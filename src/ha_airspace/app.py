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

import httpx
import structlog

from ha_airspace import __version__
from ha_airspace.alerts import AlertEvaluator
from ha_airspace.config import Config
from ha_airspace.databases import DatabaseLoader, DatabaseStore
from ha_airspace.drone_registry import DroneRegistry
from ha_airspace.enrichment import Enricher
from ha_airspace.journal import Journal
from ha_airspace.merger import Merger
from ha_airspace.metrics import MetricsRegistry
from ha_airspace.mqtt.client import MqttClient
from ha_airspace.mqtt.publisher import Publisher
from ha_airspace.orbit import ORBIT_FLAG, OrbitDetector
from ha_airspace.photos import PhotoEnricher
from ha_airspace.receivers import (
    HttpJsonReceiver,
    ReceiverSource,
    RemoteIdHttpReceiver,
)
from ha_airspace.spoof import SPOOF_FLAG, SpoofDetector
from ha_airspace.tracker import AircraftTracker

if TYPE_CHECKING:
    from ha_airspace.models import ReceiverLocation

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
        db_loader: DatabaseLoader | None = None,
        journal: Journal | None = None,
        photo_client: httpx.AsyncClient | None = None,
        drone_client: httpx.AsyncClient | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._config = config
        self._receivers = receivers
        self._client = mqtt_client
        self._publisher = publisher
        self._tracker = tracker
        self._db_loader = db_loader
        self._journal = journal
        # Long-lived enrichment HTTP clients; closed on shutdown. None unless the
        # respective feature (photos / drone registry) is enabled.
        self._photo_client = photo_client
        self._drone_client = drone_client
        self._metrics = metrics

        # Receiver name -> cached self-reported location (fetched once at
        # startup, republished on every broker (re)connect).
        self._locations: dict[str, ReceiverLocation] = {}
        self._stop = asyncio.Event()
        # Serializes merger mutation: each poll loop ingests its batch under
        # this lock, and the pipeline loop holds it across a tick. Ingest is
        # sync + fast and publish just enqueues, so contention is negligible.
        self._tracker_lock = asyncio.Lock()

        # Resolve each receiver's poll interval once (receiver override else
        # service default), keyed by name. Unknown names fall back to the
        # service default — defensive, since names validate unique at load.
        self._intervals: dict[str, float] = {
            rc.name: config.poll_interval_for(rc) for rc in config.receivers
        }
        # Remote ID feeds resolve the same way (own interval else service default).
        default_interval = config.service.poll_interval_s
        for rc in config.remoteid:
            self._intervals[rc.name] = (
                rc.poll_interval_s if rc.poll_interval_s is not None else default_interval
            )

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
        # Open + warm-load the journal BEFORE any poll runs, so first_seen is
        # restorable on the very first sighting after a restart.
        if self._journal is not None:
            await self._journal.open()
            await self._journal.warm_load()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._client.run(), name="mqtt-client")
                if self._db_loader is not None:
                    tg.create_task(self._db_loader.run(), name="db-loader")
                if self._journal is not None:
                    tg.create_task(self._journal.run(), name="journal")
                for receiver in self._receivers:
                    tg.create_task(self._poll_loop(receiver), name=f"poll-{receiver.name}")
                tg.create_task(self._pipeline_loop(), name="pipeline")
                tg.create_task(self._stop_watcher(), name="stop-watcher")
        finally:
            await self._close_receivers()
            if self._journal is not None:
                await self._journal.close()  # final flush
            for enrich_client in (self._photo_client, self._drone_client):
                if enrich_client is not None:
                    with contextlib.suppress(Exception):
                        await enrich_client.aclose()
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
        """One fetch → merge ingest → receiver-status publish cycle. The base
        receiver swallows transient failures (returns ``[]`` and marks
        itself unhealthy), so this never crashes the loop on a flaky feed.

        Ingest only — the per-cycle pipeline (lifecycle, alerts, summary) runs
        in ``_pipeline_loop`` over the merged view of all receivers, so two
        receivers seeing the same hex become one canonical aircraft."""
        observations = await receiver.fetch()
        async with self._tracker_lock:
            for obs in observations:
                self._tracker.ingest(obs)
        health = await receiver.health()
        await self._publisher.publish_receiver_status(receiver.name, online=bool(health["online"]))
        await self._publisher.publish_receiver_stats(receiver.name, health)

    # ------------------------------------------------------------------
    # Pipeline loop (single task — lifecycle/alerts/summary over merged view)
    # ------------------------------------------------------------------

    async def _pipeline_loop(self) -> None:
        """Run the merged-state pipeline on the service cadence: lifecycle,
        alert evaluation, summary, and metrics over every receiver's merged
        output. Ticking once centrally (rather than per receiver) means a hex
        seen by two receivers is published once, as one canonical aircraft.

        The tick holds the merger lock so it never runs while a poll loop is
        mutating the merged state mid-cycle."""
        interval = self._config.service.poll_interval_s
        while not self._stop.is_set():
            async with self._tracker_lock:
                await self._tracker.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

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
        if self._db_loader is not None:
            await self._db_loader.stop()
        if self._journal is not None:
            await self._journal.stop()
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
    # Drone Remote ID feeds: same poll-loop + merger machinery, band="remoteid".
    receivers.extend(
        RemoteIdHttpReceiver(
            rc.name,
            rc.url,
            timeout_s=config.service.http_timeout_s,
            metrics=metrics,
        )
        for rc in config.remoteid
        if rc.enabled
    )
    if not receivers:
        raise ValueError("no enabled receivers in config")

    client = MqttClient(config.mqtt, metrics=metrics)
    publisher = Publisher(client, config, metrics=metrics)

    # Reference DBs: only when sources are configured. The store is shared
    # between the loader (writes on refresh) and the enricher (reads per poll).
    db_loader: DatabaseLoader | None = None
    db_store: DatabaseStore | None = None
    if any(s.enabled for s in config.databases.sources):
        db_store = DatabaseStore()
        db_loader = DatabaseLoader(config.databases, db_store)

    # Alerts: only when rules are configured. The evaluator resolves
    # watchpoint elevation for the v1 AGL approximation.
    alerts: AlertEvaluator | None = None
    if config.enrichment.alerts.rules:
        elevations = {wp.name: wp.elevation_m for wp in config.watchpoints}
        alerts = AlertEvaluator(
            config.enrichment.alerts,
            elevation_m_for=elevations.get,
        )

    # Journal: only when configured. The merger restores first_seen from it
    # (in-memory after warm-load); the tracker records track summaries to it.
    # App.run() opens + warm-loads it before the poll loop and runs its writer.
    journal: Journal | None = Journal(config.journal) if config.journal is not None else None
    merger = Merger(
        first_seen_for=journal.first_seen_for if journal is not None else None,
        last_seen_for=journal.last_seen_for if journal is not None else None,
    )

    # Photo enrichment: only when enabled. A dedicated long-lived client with a
    # descriptive User-Agent (Planespotters asks for one) and a short timeout so
    # a slow lookup never holds up an alert. App closes the client on shutdown.
    user_agent = f"ha-airspace/{__version__} (+https://github.com/ifnull/ha-airspace)"

    photo_client: httpx.AsyncClient | None = None
    photos: PhotoEnricher | None = None
    if config.photos.enabled:
        photo_client = httpx.AsyncClient(
            timeout=config.service.http_timeout_s,
            headers={"User-Agent": user_agent},
        )
        photos = PhotoEnricher(
            photo_client,
            cache_ttl_s=config.photos.cache_ttl_days * 86400.0,
        )

    # FAA UAS make/model lookup for drones — its own long-lived client, same
    # pattern as photos. Closed on shutdown.
    drone_client: httpx.AsyncClient | None = None
    drone_registry: DroneRegistry | None = None
    if config.drone_registry.enabled:
        drone_client = httpx.AsyncClient(
            timeout=config.service.http_timeout_s,
            headers={"User-Agent": user_agent},
        )
        drone_registry = DroneRegistry(
            drone_client,
            cache_ttl_s=config.drone_registry.cache_ttl_days * 86400.0,
        )

    orbit = OrbitDetector(config.orbit) if config.orbit.enabled else None
    spoof = SpoofDetector(config.spoof) if config.spoof.enabled else None

    # Flags that get a per-flag feed sensor: every configured flag, plus the
    # derived 'orbiting' / 'spoof_suspect' flags when their detectors are on.
    # Kept in sync with the discovery side (build_discovery_payloads enumerates
    # the same set).
    feed_flags = list(config.enrichment.flags)
    if config.orbit.enabled:
        feed_flags.append(ORBIT_FLAG)
    if config.spoof.enabled:
        feed_flags.append(SPOOF_FLAG)

    tracker = AircraftTracker(
        publisher,
        config.watchpoints_runtime(),
        merger=merger,
        enricher=Enricher(config.enrichment, db_store=db_store),
        alerts=alerts,
        journal=journal,
        photos=photos,
        orbit=orbit,
        spoof=spoof,
        drone_registry=drone_registry,
        has_drone_source=any(rc.enabled for rc in config.remoteid),
        feed_flags=feed_flags,
        metrics=metrics,
    )
    return App(
        config,
        receivers=receivers,
        mqtt_client=client,
        publisher=publisher,
        tracker=tracker,
        db_loader=db_loader,
        journal=journal,
        photo_client=photo_client,
        drone_client=drone_client,
        metrics=metrics,
    )


__all__ = ["App", "build_app"]
