"""Integration-test fixtures: a real Mosquitto broker via testcontainers.

Session-scoped so the container starts once per ``pytest -m integration`` run
(pulling eclipse-mosquitto is a one-time cost; starting it is ~1s). Anonymous
access is enabled through a minimal mosquitto.conf bind-mounted into the
container — the default image refuses anonymous connections.

These tests are skipped by default (``-m "not integration"`` in pyproject) and
require Docker. Run them with ``uv run pytest -m integration``.

A ``BrokerProbe`` helper subscribes with an independent aiomqtt client so a
test can assert on what the service actually published — including retained
state, which is the whole point of testing against a real broker rather than
the in-memory fake.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

import aiomqtt
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

_MOSQUITTO_IMAGE = "eclipse-mosquitto:2"
_MQTT_PORT = 1883

_MOSQUITTO_CONF = """\
listener 1883
allow_anonymous true
"""


@dataclass(frozen=True)
class BrokerEndpoint:
    """Where the test broker is reachable on the host."""

    host: str
    port: int


@pytest.fixture(scope="session")
def mosquitto() -> Iterator[BrokerEndpoint]:
    """Start a Mosquitto container once per session; yield its host endpoint.

    Bind-mounts a minimal anonymous-access config. Waits for the broker's
    'running' log line so tests never race the listener coming up.
    """
    with TemporaryDirectory() as tmp:
        conf_path = Path(tmp) / "mosquitto.conf"
        conf_path.write_text(_MOSQUITTO_CONF, encoding="utf-8")

        container = (
            DockerContainer(_MOSQUITTO_IMAGE)
            .with_exposed_ports(_MQTT_PORT)
            .with_volume_mapping(str(conf_path), "/mosquitto/config/mosquitto.conf", "ro")
        )
        container.start()
        try:
            # String predicate is matched as a regex via re.search. The
            # testcontainers deprecation note prefers structured wait
            # strategies; the string form is stable enough for one log line.
            wait_for_logs(container, "mosquitto version 2", timeout=30)
            host = container.get_container_host_ip()
            port = int(container.get_exposed_port(_MQTT_PORT))
            yield BrokerEndpoint(host=host, port=port)
        finally:
            container.stop()


@dataclass
class _Captured:
    """One received MQTT message."""

    topic: str
    payload: bytes
    retain: bool


@dataclass
class BrokerProbe:
    """Independent subscriber for asserting on broker state.

    Subscribes with its own aiomqtt session (separate from the service under
    test) so it sees exactly what a real consumer like Home Assistant would —
    including messages retained before the probe connected.
    """

    endpoint: BrokerEndpoint
    messages: list[_Captured] = field(default_factory=list)
    _task: asyncio.Task[None] | None = None
    _client: aiomqtt.Client | None = None
    _ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def subscribe(self, topic_filter: str = "#") -> None:
        """Begin collecting messages matching ``topic_filter`` in the
        background. Returns once the subscription is active."""
        self._task = asyncio.create_task(self._run(topic_filter))
        await asyncio.wait_for(self._ready.wait(), timeout=10)

    async def _run(self, topic_filter: str) -> None:
        async with aiomqtt.Client(self.endpoint.host, port=self.endpoint.port) as client:
            self._client = client
            await client.subscribe(topic_filter)
            self._ready.set()
            async for message in client.messages:
                self.messages.append(
                    _Captured(
                        topic=str(message.topic),
                        payload=(
                            message.payload
                            if isinstance(message.payload, bytes)
                            else bytes(str(message.payload), "utf-8")
                        ),
                        retain=message.retain,
                    )
                )

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def topics(self) -> list[str]:
        return [m.topic for m in self.messages]

    def latest(self, topic: str) -> _Captured | None:
        """Most recent message on an exact topic, or None."""
        for msg in reversed(self.messages):
            if msg.topic == topic:
                return msg
        return None


@pytest.fixture
async def probe(mosquitto: BrokerEndpoint) -> AsyncIterator[BrokerProbe]:
    """A connected BrokerProbe, torn down after the test."""
    p = BrokerProbe(endpoint=mosquitto)
    try:
        yield p
    finally:
        await p.aclose()
