"""Full-stack integration: App -> real Mosquitto -> HA-like subscriber.

Drives the actual App (FileReceiver replaying a captured aircraft.json,
real MqttClient + Publisher + AircraftTracker) against a live broker, then
subscribes a fresh probe to assert on the retained state a freshly-started
Home Assistant would see. This is the Phase 1 "done when" proven over a real
broker: a receiver in, a retained nearest-aircraft + discovery entities out.

Skipped by default; run with ``uv run pytest -m integration`` (needs Docker).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ha_airspace.app import App
from ha_airspace.config import Config
from ha_airspace.mqtt.client import MqttClient
from ha_airspace.mqtt.publisher import Publisher
from ha_airspace.receivers import FileReceiver
from ha_airspace.tracker import AircraftTracker
from tests.integration.conftest import BrokerEndpoint, BrokerProbe

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _config(endpoint: BrokerEndpoint, *, base_topic: str) -> Config:
    return Config.model_validate(
        {
            "service": {"poll_interval_s": 0.05},
            "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
            "mqtt": {
                "broker": endpoint.host,
                "port": endpoint.port,
                "base_topic": base_topic,
            },
            "receivers": [
                {"name": "rx-home", "url": "http://unused/aircraft.json", "band": "1090"}
            ],
        }
    )


def _build_app(config: Config) -> App:
    """Build a real App but with a FileReceiver (no network) instead of
    HttpJsonReceiver. Everything downstream of the receiver is real."""
    client = MqttClient(config.mqtt)
    publisher = Publisher(client, config)
    tracker = AircraftTracker(publisher, config.watchpoints_runtime())
    receiver = FileReceiver("rx-home", "1090", _FIXTURES / "aircraft_basic.json")
    return App(
        config,
        receivers=[receiver],
        mqtt_client=client,
        publisher=publisher,
        tracker=tracker,
    )


async def _wait_until(
    predicate: Callable[[], bool], *, timeout_s: float = 15.0, poll_s: float = 0.02
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(poll_s)


class TestFullStack:
    async def test_nearest_and_summary_retained_for_late_subscriber(
        self, mosquitto: BrokerEndpoint, probe: BrokerProbe
    ) -> None:
        config = _config(mosquitto, base_topic="e2e1")
        app = _build_app(config)

        run_task = asyncio.create_task(app.run())
        # Let it connect + run several poll cycles so retained state settles.
        await asyncio.sleep(0.6)
        app.request_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=10)

        # A freshly-started HA: subscribe now, read retained state.
        await probe.subscribe("e2e1/#")
        await _wait_until(lambda: probe.latest("e2e1/summary/count") is not None)

        count = probe.latest("e2e1/summary/count")
        assert count is not None
        assert count.retain is True
        # aircraft_basic.json has 2 aircraft.
        assert count.payload == b"2"

        nearest = probe.latest("e2e1/summary/nearest")
        assert nearest is not None
        assert nearest.retain is True
        body = json.loads(nearest.payload)
        # Nearest must carry a hex and a home distance.
        assert "hex" in body
        assert "home" in body["distance_to"]

    async def test_discovery_published_for_late_subscriber(
        self, mosquitto: BrokerEndpoint, probe: BrokerProbe
    ) -> None:
        config = _config(mosquitto, base_topic="e2e2")
        app = _build_app(config)

        run_task = asyncio.create_task(app.run())
        await asyncio.sleep(0.4)
        app.request_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=10)

        # HA discovery lands on the homeassistant/ prefix, retained.
        await probe.subscribe("homeassistant/#")
        await _wait_until(lambda: any(t.endswith("/config") for t in probe.topics()))

        config_topics = [m for m in probe.messages if m.topic.endswith("/config")]
        assert config_topics
        for msg in config_topics:
            assert msg.retain is True
            body = json.loads(msg.payload)
            assert "unique_id" in body
            assert "device" in body

    async def test_status_online_retained_then_offline_on_stop(
        self, mosquitto: BrokerEndpoint, probe: BrokerProbe
    ) -> None:
        config = _config(mosquitto, base_topic="e2e3")
        app = _build_app(config)

        # Subscribe live to watch the online->offline transition.
        await probe.subscribe("e2e3/status")

        run_task = asyncio.create_task(app.run())
        await _wait_until(
            lambda: any(m.topic == "e2e3/status" and m.payload == b"online" for m in probe.messages)
        )
        app.request_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=10)

        def _offline_seen() -> bool:
            latest = probe.latest("e2e3/status")
            return latest is not None and latest.payload == b"offline"

        await _wait_until(_offline_seen)
        final = probe.latest("e2e3/status")
        assert final is not None
        assert final.payload == b"offline"
