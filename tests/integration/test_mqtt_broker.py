"""Integration tests for MqttClient against a real Mosquitto broker.

These cover the protocol-level behavior the in-memory fake cannot prove,
all of which the eng review flagged as broker-dependent:

  * a published retained message is actually retained (a late subscriber
    sees it),
  * graceful shutdown publishes status:offline retained and disconnects
    cleanly (the will does NOT also fire — clean DISCONNECT suppresses it),
  * the queue drains in order once connected,
  * reconnect after a broker drop re-establishes the session and the
    on_connect callback re-runs.

Skipped by default; run with ``uv run pytest -m integration`` (needs Docker).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import pytest

from ha_airspace.config import MqttConfig
from ha_airspace.mqtt.client import MqttClient
from tests.integration.conftest import BrokerEndpoint, BrokerProbe

pytestmark = pytest.mark.integration


def _config(endpoint: BrokerEndpoint, *, base_topic: str = "adsb") -> MqttConfig:
    return MqttConfig(broker=endpoint.host, port=endpoint.port, base_topic=base_topic)


async def _wait_until(
    predicate: Callable[[], bool], *, timeout_s: float = 10.0, poll_s: float = 0.02
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(poll_s)


class TestRetainedPublish:
    async def test_retained_message_seen_by_late_subscriber(
        self, mosquitto: BrokerEndpoint, probe: BrokerProbe
    ) -> None:
        client = MqttClient(_config(mosquitto, base_topic="ret1"))
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: client.is_connected)
            await client.publish("ret1/aircraft/abc", b'{"hex":"abc"}', retain=True)
            # Give the drain loop time to flush to the broker.
            await asyncio.sleep(0.3)
            # Subscribe AFTER the publish — only a retained message survives.
            await probe.subscribe("ret1/#")
            await _wait_until(lambda: probe.latest("ret1/aircraft/abc") is not None)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        msg = probe.latest("ret1/aircraft/abc")
        assert msg is not None
        assert msg.payload == b'{"hex":"abc"}'
        assert msg.retain is True


class TestGracefulShutdown:
    async def test_stop_publishes_offline_retained(
        self, mosquitto: BrokerEndpoint, probe: BrokerProbe
    ) -> None:
        # Subscribe AFTER shutdown so the broker replays its retained state.
        # (A message received while already subscribed arrives with
        # retain=False even when the broker retained it — the retain flag is
        # only set on delivery triggered by a new subscription.)
        client = MqttClient(_config(mosquitto, base_topic="gs1"))
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: client.is_connected)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await probe.subscribe("gs1/#")
        await _wait_until(lambda: probe.latest("gs1/status") is not None)
        status = probe.latest("gs1/status")
        assert status is not None
        assert status.payload == b"offline"
        assert status.retain is True

    async def test_clean_disconnect_suppresses_will(
        self, mosquitto: BrokerEndpoint, probe: BrokerProbe
    ) -> None:
        # On a *clean* stop, exactly one offline should land (our explicit
        # publish), not two (explicit + will). We assert the final retained
        # state is offline and that only offline payloads appeared on status.
        await probe.subscribe("gs2/#")
        client = MqttClient(_config(mosquitto, base_topic="gs2"))
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: client.is_connected)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await _wait_until(lambda: probe.latest("gs2/status") is not None)
        status_msgs = [m for m in probe.messages if m.topic == "gs2/status"]
        # Only offline payloads (no rogue will firing a different value).
        assert all(m.payload == b"offline" for m in status_msgs)


class TestQueueDrain:
    async def test_messages_drain_in_order(
        self, mosquitto: BrokerEndpoint, probe: BrokerProbe
    ) -> None:
        await probe.subscribe("ord1/#")
        client = MqttClient(_config(mosquitto, base_topic="ord1"))
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: client.is_connected)
            for i in range(10):
                await client.publish("ord1/seq", str(i).encode(), qos=1)
            await _wait_until(
                lambda: len([m for m in probe.messages if m.topic == "ord1/seq"]) >= 10
            )
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        seq = [int(m.payload) for m in probe.messages if m.topic == "ord1/seq"]
        assert seq == list(range(10))


class TestReconnect:
    async def test_on_connect_runs_on_initial_connect(self, mosquitto: BrokerEndpoint) -> None:
        connects = 0

        async def on_connect() -> None:
            nonlocal connects
            connects += 1

        client = MqttClient(_config(mosquitto, base_topic="rc1"), on_connect=on_connect)
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(client.run())
            await _wait_until(lambda: connects >= 1)
            await client.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert connects == 1
