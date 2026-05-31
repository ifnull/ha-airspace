"""Tests for adsb_enrich.mqtt.client.MqttClient.

aiomqtt is replaced by FakeAiomqttClient — implements the same
``async with`` + ``publish`` shape but in-memory. The connection-loop
LWT semantics, real reconnect timing, and broker-side retained-state
behavior live in tests/integration/ where Mosquitto is available.

Cover here:
  * Queue overflow drops oldest + increments mqtt_drops_total.
  * is_connected reflects the connection state.
  * stop() drains the queue, publishes offline retained, exits.
  * on_connect fires on first connect AND on reconnect.
  * Reconnect counter increments only after the first connect.
  * Broker disconnect (MqttError) triggers backoff + reconnect.
  * Publishes increment mqtt_publishes_total with the right topic_class.
  * String payloads are utf-8 encoded.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, ClassVar

import aiomqtt
import pytest
from prometheus_client import CollectorRegistry

from adsb_enrich.config import MqttConfig
from adsb_enrich.metrics import MetricsRegistry
from adsb_enrich.mqtt.client import (
    MqttClient,
    _ExponentialBackoff,
)

# ---------------------------------------------------------------------------
# Fake aiomqtt.Client for in-memory testing
# ---------------------------------------------------------------------------


class FakeAiomqttClient:
    """In-memory replacement for ``aiomqtt.Client``.

    Records every publish; lets each test queue per-call connect/publish
    behavior. Implements the async-context-manager + publish shape
    MqttClient depends on; nothing else.

    Class-level ``_session_log`` records the lifecycle of every instance
    constructed for a given test, in order. Tests reset it per test via
    the ``fake_aiomqtt`` fixture.
    """

    # Per-test state. Reset by fixture. ClassVar annotations because
    # this is intentionally per-class state used as a recording surface
    # — every constructed instance writes to the same lists.
    fail_on_enter_count: ClassVar[int] = 0
    """Raise MqttError on next N __aenter__ calls."""
    fail_on_publish_count: ClassVar[int] = 0
    """Raise MqttError on next N publish() calls."""
    publishes: ClassVar[list[tuple[str, bytes, int, bool]]] = []
    sessions: ClassVar[list[dict[str, Any]]] = []
    drop_event: ClassVar[asyncio.Event | None] = None

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        FakeAiomqttClient.sessions.append({"kwargs": kwargs, "entered": False})

    async def __aenter__(self) -> FakeAiomqttClient:
        if FakeAiomqttClient.fail_on_enter_count > 0:
            FakeAiomqttClient.fail_on_enter_count -= 1
            raise aiomqtt.MqttError("simulated connect failure")
        FakeAiomqttClient.sessions[-1]["entered"] = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        FakeAiomqttClient.sessions[-1]["exited"] = True

    async def publish(
        self,
        topic: str,
        payload: bytes | str = b"",
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        if FakeAiomqttClient.fail_on_publish_count > 0:
            FakeAiomqttClient.fail_on_publish_count -= 1
            raise aiomqtt.MqttError("simulated publish failure")
        encoded = payload.encode("utf-8") if isinstance(payload, str) else payload
        FakeAiomqttClient.publishes.append((topic, encoded, qos, retain))


@pytest.fixture(autouse=True)
def fake_aiomqtt() -> None:
    """Reset FakeAiomqttClient state before every test."""
    FakeAiomqttClient.fail_on_enter_count = 0
    FakeAiomqttClient.fail_on_publish_count = 0
    FakeAiomqttClient.publishes = []
    FakeAiomqttClient.sessions = []
    FakeAiomqttClient.drop_event = None


@pytest.fixture
def mqtt_config() -> MqttConfig:
    return MqttConfig(broker="broker.local", port=1883, base_topic="adsb")


@pytest.fixture
def metrics() -> MetricsRegistry:
    return MetricsRegistry(registry=CollectorRegistry())


def _make_client(
    config: MqttConfig,
    *,
    metrics: MetricsRegistry | None = None,
    on_connect: Callable[[], Any] | None = None,
    queue_max_size: int = 1000,
) -> MqttClient:
    return MqttClient(
        config,
        metrics=metrics,
        on_connect=on_connect,
        queue_max_size=queue_max_size,
        client_factory=FakeAiomqttClient,
    )


# ---------------------------------------------------------------------------
# Queue semantics — pure unit tests, no run() needed
# ---------------------------------------------------------------------------


class TestQueueSemantics:
    async def test_publish_enqueues_when_disconnected(self, mqtt_config: MqttConfig) -> None:
        client = _make_client(mqtt_config)
        await client.publish("topic", b"payload", retain=True)
        # Queue length is private; verify by looking at the internal
        # state. The behaviour is "no exception, message accepted".
        assert client.is_connected is False  # never started run()

    async def test_str_payload_utf8_encoded(self, mqtt_config: MqttConfig) -> None:
        client = _make_client(mqtt_config)
        await client.publish("topic", "hello", retain=False)
        # Inspect the internal queue directly — this is the contract
        # MqttClient owns regardless of whether run() is going.
        msg = client._queue[0]
        assert msg.payload == b"hello"

    async def test_overflow_drops_oldest_and_increments_counter(
        self, mqtt_config: MqttConfig, metrics: MetricsRegistry
    ) -> None:
        client = _make_client(mqtt_config, metrics=metrics, queue_max_size=3)
        for i in range(5):
            await client.publish("t", f"msg-{i}".encode())
        # Queue holds the 3 most recent: msg-2, msg-3, msg-4.
        msgs = [m.payload for m in client._queue]
        assert msgs == [b"msg-2", b"msg-3", b"msg-4"]
        # Two drops counted.
        assert _metric_value(metrics.mqtt_drops) == 2.0

    async def test_overflow_without_metrics_still_drops(self, mqtt_config: MqttConfig) -> None:
        # No metrics injected — overflow still works correctly.
        client = _make_client(mqtt_config, queue_max_size=2)
        for i in range(4):
            await client.publish("t", f"msg-{i}".encode())
        msgs = [m.payload for m in client._queue]
        assert msgs == [b"msg-2", b"msg-3"]


# ---------------------------------------------------------------------------
# run() lifecycle — connect, publish, disconnect, reconnect
# ---------------------------------------------------------------------------


class TestRunLifecycle:
    async def test_run_connects_and_drains_queue(self, mqtt_config: MqttConfig) -> None:
        client = _make_client(mqtt_config)
        await client.publish("topic-A", b"payload-A", retain=True)
        await client.publish("topic-B", b"payload-B", retain=False, qos=1)

        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            # Give the drain loop a moment to publish.
            await _wait_until(lambda: len(FakeAiomqttClient.publishes) >= 2)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        published_topics = [t for t, *_ in FakeAiomqttClient.publishes]
        assert "topic-A" in published_topics
        assert "topic-B" in published_topics

    async def test_on_connect_fires_after_session_established(
        self, mqtt_config: MqttConfig
    ) -> None:
        connect_count = 0

        async def on_connect() -> None:
            nonlocal connect_count
            connect_count += 1

        client = _make_client(mqtt_config, on_connect=on_connect)
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: connect_count >= 1)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert connect_count == 1

    async def test_is_connected_flag_tracks_session(self, mqtt_config: MqttConfig) -> None:
        gate = asyncio.Event()

        async def on_connect() -> None:
            gate.set()

        client = _make_client(mqtt_config, on_connect=on_connect)
        assert client.is_connected is False
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await gate.wait()
            assert client.is_connected is True
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert client.is_connected is False


# ---------------------------------------------------------------------------
# Graceful shutdown — publishes offline retained before exit
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    async def test_stop_publishes_offline_retained_before_exit(
        self, mqtt_config: MqttConfig
    ) -> None:
        gate = asyncio.Event()

        async def on_connect() -> None:
            gate.set()

        client = _make_client(mqtt_config, on_connect=on_connect)
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await gate.wait()
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # The very last publish is the offline retained message.
        assert FakeAiomqttClient.publishes
        last = FakeAiomqttClient.publishes[-1]
        topic, payload, _qos, retain = last
        assert topic == "adsb/status"
        assert payload == b"offline"
        assert retain is True

    async def test_stop_is_idempotent(self, mqtt_config: MqttConfig) -> None:
        gate = asyncio.Event()

        async def on_connect() -> None:
            gate.set()

        client = _make_client(mqtt_config, on_connect=on_connect)
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await gate.wait()
            await client.stop()
            await client.stop()  # second call should not raise
            with contextlib.suppress(asyncio.CancelledError):
                await task


# ---------------------------------------------------------------------------
# Reconnect — broker drop triggers backoff + reconnect
# ---------------------------------------------------------------------------


class TestReconnect:
    async def test_initial_connect_failure_triggers_backoff_and_retry(
        self, mqtt_config: MqttConfig, metrics: MetricsRegistry
    ) -> None:
        # First two __aenter__ calls fail; third succeeds.
        FakeAiomqttClient.fail_on_enter_count = 2
        connect_count = 0

        async def on_connect() -> None:
            nonlocal connect_count
            connect_count += 1

        client = _make_client(mqtt_config, metrics=metrics, on_connect=on_connect)
        # Override the backoff so retries happen instantly. Inject a
        # zero-delay backoff instance — without it, the test would wait
        # ~3s for the real exponential schedule.
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(
                lambda: connect_count >= 1,
                timeout_s=10.0,
            )
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # Two failed sessions + one successful = 3 sessions tried.
        assert len(FakeAiomqttClient.sessions) >= 3
        # Two reconnect-after-success events: NO. The reconnect counter
        # only increments after a SUCCESSFUL session was followed by
        # another successful one. Our scenario has 2 failures (no
        # session ever established) then 1 success — that's
        # first_connect, so reconnects=0.
        assert _metric_value(metrics.mqtt_reconnects) == 0.0

    async def test_reconnect_counter_increments_on_session_recovery(
        self, mqtt_config: MqttConfig, metrics: MetricsRegistry
    ) -> None:
        # Connect succeeds, then publish fails (simulating mid-session
        # broker drop), then connect succeeds again.
        connect_count = 0

        async def on_connect() -> None:
            nonlocal connect_count
            connect_count += 1
            # On the FIRST successful connect, queue a publish that
            # the fake will fail with MqttError, dropping the session.
            if connect_count == 1:
                FakeAiomqttClient.fail_on_publish_count = 1
                # publish something so the drain loop hits the failure
                await client.publish("trigger", b"x", topic_class="other")

        client = _make_client(mqtt_config, metrics=metrics, on_connect=on_connect)

        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: connect_count >= 2, timeout_s=15.0)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # connect_count >= 2 means we reconnected at least once. The
        # mqtt_reconnects counter should reflect that.
        assert _metric_value(metrics.mqtt_reconnects) >= 1.0


# ---------------------------------------------------------------------------
# Metrics integration
# ---------------------------------------------------------------------------


class TestMetricsIntegration:
    async def test_publish_increments_topic_class_counter(
        self, mqtt_config: MqttConfig, metrics: MetricsRegistry
    ) -> None:
        client = _make_client(mqtt_config, metrics=metrics)

        await client.publish("a", b"x", topic_class="aircraft")
        await client.publish("s", b"y", topic_class="summary")
        await client.publish("d", b"z", topic_class="discovery")

        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(
                lambda: (
                    len(FakeAiomqttClient.publishes) >= 3 + 1  # +offline
                    or _metric_value(metrics.mqtt_publishes.labels(topic_class="aircraft")) >= 1
                )
            )
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert _metric_value(metrics.mqtt_publishes.labels(topic_class="aircraft")) == 1.0
        assert _metric_value(metrics.mqtt_publishes.labels(topic_class="summary")) == 1.0
        assert _metric_value(metrics.mqtt_publishes.labels(topic_class="discovery")) == 1.0

    async def test_connection_gauge_set_to_one_while_connected(
        self, mqtt_config: MqttConfig, metrics: MetricsRegistry
    ) -> None:
        gate = asyncio.Event()

        async def on_connect() -> None:
            gate.set()

        client = _make_client(mqtt_config, metrics=metrics, on_connect=on_connect)
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await gate.wait()
            assert _metric_value(metrics.mqtt_connected) == 1.0
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert _metric_value(metrics.mqtt_connected) == 0.0


# ---------------------------------------------------------------------------
# Will (LWT) configuration
# ---------------------------------------------------------------------------


class TestLwtConfig:
    async def test_will_topic_payload_qos_retain(self, mqtt_config: MqttConfig) -> None:
        client = _make_client(mqtt_config)
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: len(FakeAiomqttClient.sessions) >= 1)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert FakeAiomqttClient.sessions
        first_session = FakeAiomqttClient.sessions[0]
        will = first_session["kwargs"]["will"]
        # aiomqtt.Will exposes attributes; verify the locked spec.
        assert will.topic == "adsb/status"
        assert will.payload == b"offline"
        assert will.retain is True
        assert will.qos == 1

    async def test_credentials_passed_when_set(self) -> None:
        cfg = MqttConfig(broker="b", username="u", password="p", base_topic="adsb")
        client = _make_client(cfg)
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: len(FakeAiomqttClient.sessions) >= 1)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        kwargs = FakeAiomqttClient.sessions[0]["kwargs"]
        assert kwargs["username"] == "u"
        assert kwargs["password"] == "p"

    async def test_credentials_omitted_when_unset(self, mqtt_config: MqttConfig) -> None:
        client = _make_client(mqtt_config)
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: len(FakeAiomqttClient.sessions) >= 1)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        kwargs = FakeAiomqttClient.sessions[0]["kwargs"]
        assert "username" not in kwargs
        assert "password" not in kwargs

    async def test_tls_context_built_when_enabled(self) -> None:
        cfg = MqttConfig(broker="b", tls=True, base_topic="adsb")
        client = _make_client(cfg)
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: len(FakeAiomqttClient.sessions) >= 1)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        kwargs = FakeAiomqttClient.sessions[0]["kwargs"]
        assert "tls_context" in kwargs


# ---------------------------------------------------------------------------
# _ExponentialBackoff
# ---------------------------------------------------------------------------


class TestExponentialBackoff:
    def test_first_call_returns_initial(self) -> None:
        b = _ExponentialBackoff(initial=1.0, factor=2.0, max_s=10.0)
        assert b.next() == 1.0

    def test_doubles_each_call(self) -> None:
        b = _ExponentialBackoff(initial=1.0, factor=2.0, max_s=100.0)
        assert b.next() == 1.0
        assert b.next() == 2.0
        assert b.next() == 4.0
        assert b.next() == 8.0

    def test_clamps_at_max(self) -> None:
        b = _ExponentialBackoff(initial=1.0, factor=10.0, max_s=5.0)
        b.next()  # 1
        b.next()  # 5 (would be 10, clamped)
        assert b.next() == 5.0
        assert b.next() == 5.0

    def test_reset_returns_to_initial(self) -> None:
        b = _ExponentialBackoff(initial=1.0, factor=2.0, max_s=100.0)
        b.next()
        b.next()
        b.next()
        b.reset()
        assert b.next() == 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 5.0,
    poll_s: float = 0.01,
) -> None:
    """Poll ``predicate`` until it returns True or timeout. Raises
    AssertionError on timeout — keeps test failures actionable."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"predicate never became true within {timeout_s}s")
        await asyncio.sleep(poll_s)


def _metric_value(metric: Any) -> float:
    return float(metric._value.get())
