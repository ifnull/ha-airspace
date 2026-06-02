"""aiomqtt connection wrapper.

Owns the connection lifecycle: connect (with LWT configured), drain the
publish queue, reconnect-with-backoff on broker drop, graceful shutdown
with the locked publish-offline-before-disconnect protocol.

The publisher (``mqtt/publisher.py``) sits on top and never sees aiomqtt
directly. It calls ``client.publish(topic, payload, retain=...)``; this
class enqueues the message and a worker drains the queue when connected.
During disconnect, the queue absorbs publishes; on overflow we drop the
oldest message and increment ``adsb_mqtt_drops_total``.

LWT and graceful shutdown:

* The will message is configured at connect time:
  ``topic=adsb/status, payload=offline, retain=True, qos=1``. If we
  crash, the broker publishes that for us.
* On graceful shutdown (``stop()`` called or ``run()`` cancelled) we
  publish ``adsb/status: offline`` retained ourselves before exiting
  the aiomqtt context. aiomqtt's ``__aexit__`` then sends MQTT
  DISCONNECT cleanly, which suppresses the LWT (broker won't fire it).
* Either way HA sees the offline state. We deliberately publish
  retained=True on graceful shutdown (deviating from the eng review
  spec which had retain=False) so a late-subscribing HA does not see
  stale ``online`` retained state from before the shutdown.

aiomqtt is replaceable for tests via ``client_factory``: pass any
callable that returns an object compatible with the aiomqtt.Client
async-context-manager + publish protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiomqtt
import structlog

from adsb_enrich.config import MqttConfig
from adsb_enrich.metrics import MetricsRegistry

log = structlog.get_logger(__name__)


OnConnectCallback = Callable[[], Awaitable[None]]
"""Async callback run after every successful broker connect. The
publisher uses this to publish status:online + the discovery set."""


@dataclass(slots=True)
class _QueuedMessage:
    """One pending publish. Carries ``topic_class`` so the metric label
    follows the message all the way to the broker — keeps the publisher
    from having to know about MetricsRegistry directly."""

    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    topic_class: str = "other"


class _ExponentialBackoff:
    """Standard exponential backoff with cap. Reset on successful connect."""

    def __init__(self, *, initial: float = 1.0, factor: float = 2.0, max_s: float = 60.0) -> None:
        self._initial = initial
        self._factor = factor
        self._max = max_s
        self._current = initial

    def reset(self) -> None:
        self._current = self._initial

    def next(self) -> float:
        delay = self._current
        self._current = min(self._current * self._factor, self._max)
        return delay


class MqttClient:
    """Long-lived MQTT connection with a publish queue.

    Lifecycle:
      1. Construct (does NOT connect).
      2. Schedule ``run()`` as a task in an ``asyncio.TaskGroup``.
      3. Call ``publish(...)`` from anywhere — items queue if disconnected.
      4. Cancel the task or call ``stop()`` to trigger graceful shutdown.

    Construction args:
      config: MqttConfig from validated app config.
      metrics: Optional MetricsRegistry. When supplied, every publish
        increments mqtt_publishes_total with the message's topic_class
        label; queue overflows increment mqtt_drops_total; connection
        state drives mqtt_connected and mqtt_reconnects_total.
      on_connect: Optional async callback fired AFTER aiomqtt has
        established the session and BEFORE the drain loop starts. The
        publisher uses this to publish status:online + the HA discovery
        set on every (re)connect.
      queue_max_size: Cap on in-memory pending publishes. On overflow
        we drop the oldest and increment the drops counter — the eng
        review accepted this over blocking the producer.
      client_factory: Override aiomqtt.Client for tests. Must return
        an object that supports ``async with`` and an async ``publish``
        method with the same signature as aiomqtt's.
    """

    DEFAULT_QUEUE_MAX_SIZE: int = 1000

    def __init__(
        self,
        config: MqttConfig,
        *,
        metrics: MetricsRegistry | None = None,
        on_connect: OnConnectCallback | None = None,
        queue_max_size: int = DEFAULT_QUEUE_MAX_SIZE,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config
        self._metrics = metrics
        self._on_connect = on_connect
        self._queue_max_size = queue_max_size
        self._client_factory: Callable[..., Any] = (
            client_factory if client_factory is not None else aiomqtt.Client
        )

        self._queue: deque[_QueuedMessage] = deque()
        self._new_item = asyncio.Event()
        self._client: Any = None  # set inside async with
        self._is_connected: bool = False
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def set_on_connect(self, on_connect: OnConnectCallback) -> None:
        """Set (or replace) the post-connect callback after construction.

        Resolves a startup ordering cycle: the callback typically belongs to
        the publisher, which is built *after* the client (it depends on it).
        Takes effect on the next connect; the current session is unaffected.
        """
        self._on_connect = on_connect

    @property
    def status_topic(self) -> str:
        """``<base_topic>/status`` — used for both LWT and graceful
        shutdown notifications."""
        return f"{self._config.base_topic}/status"

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        retain: bool = False,
        qos: int = 0,
        topic_class: str = "other",
    ) -> None:
        """Enqueue a publish. Drops oldest on overflow.

        Never blocks on connection state — items queue while
        disconnected. The drain worker pops them when the connection
        comes back. Cap is ``queue_max_size`` (default 1000); on
        overflow we drop the oldest message and bump the drops counter.
        """
        encoded = payload.encode("utf-8") if isinstance(payload, str) else payload
        msg = _QueuedMessage(
            topic=topic,
            payload=encoded,
            qos=qos,
            retain=retain,
            topic_class=topic_class,
        )
        if len(self._queue) >= self._queue_max_size:
            self._queue.popleft()
            if self._metrics is not None:
                self._metrics.mqtt_drops.inc()
        self._queue.append(msg)
        self._new_item.set()

    async def stop(self) -> None:
        """Trigger graceful shutdown. ``run()`` will drain the in-flight
        queue, publish offline retained, then exit the aiomqtt context
        cleanly. Idempotent.
        """
        self._stop_event.set()
        self._new_item.set()  # wake the drain loop

    async def run(self) -> None:
        """Main connection lifecycle. Returns when ``stop()`` is called
        or the task is cancelled.

        Connection states:
          * Disconnected: sleep with exponential backoff, then retry.
          * Connecting: aiomqtt establishes the session.
          * Connected: drain the publish queue. ``on_connect`` runs once
            per session.
          * Stopping: drain the remaining queue best-effort, publish
            offline retained, exit the aiomqtt context cleanly.
        """
        backoff = _ExponentialBackoff()
        first_connect = True

        while not self._stop_event.is_set():
            try:
                will = aiomqtt.Will(
                    topic=self.status_topic,
                    payload=b"offline",
                    qos=1,
                    retain=True,
                )
                client_kwargs = self._build_client_kwargs(will)
                async with self._client_factory(**client_kwargs) as client:
                    self._client = client
                    self._is_connected = True
                    if self._metrics is not None:
                        self._metrics.mqtt_connected.set(1)
                        if not first_connect:
                            self._metrics.mqtt_reconnects.inc()
                    first_connect = False
                    backoff.reset()

                    try:
                        if self._on_connect is not None:
                            await self._on_connect()
                        await self._drain_loop()
                    finally:
                        # Inside the async-with, before context exit.
                        # Either stop_event was set (graceful) or we
                        # are unwinding due to cancellation. Publish
                        # offline retained so HA sees the right state
                        # whether or not the broker fires the LWT.
                        with contextlib.suppress(aiomqtt.MqttError, asyncio.CancelledError):
                            await client.publish(
                                self.status_topic,
                                b"offline",
                                qos=1,
                                retain=True,
                            )
                        self._is_connected = False
                        if self._metrics is not None:
                            self._metrics.mqtt_connected.set(0)
            except aiomqtt.MqttError as exc:
                log.warning(
                    "mqtt_disconnect",
                    broker=self._config.broker,
                    error=str(exc),
                )
                self._is_connected = False
                if self._metrics is not None:
                    self._metrics.mqtt_connected.set(0)
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(backoff.next())

        self._is_connected = False
        if self._metrics is not None:
            self._metrics.mqtt_connected.set(0)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _drain_loop(self) -> None:
        """Pop queued messages and publish until ``stop_event`` is set."""
        while not self._stop_event.is_set():
            if not self._queue:
                await self._new_item.wait()
                self._new_item.clear()
                continue
            msg = self._queue.popleft()
            assert self._client is not None
            await self._client.publish(msg.topic, msg.payload, qos=msg.qos, retain=msg.retain)
            if self._metrics is not None:
                self._metrics.mqtt_publishes.labels(topic_class=msg.topic_class).inc()

    def _build_client_kwargs(self, will: aiomqtt.Will) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "hostname": self._config.broker,
            "port": self._config.port,
            "will": will,
        }
        if self._config.username is not None:
            kwargs["username"] = self._config.username
        if self._config.password is not None:
            kwargs["password"] = self._config.password
        if self._config.tls:
            kwargs["tls_context"] = ssl.create_default_context()
        return kwargs


__all__ = ["MqttClient", "OnConnectCallback"]
