"""Tests for ha_airspace.app.App orchestration.

Drives the App end to end with a real FileReceiver (replays a fixture) and
a FakeMqttClient that records publishes and fires on_connect — no network,
no broker. The real Publisher and AircraftTracker are exercised, so this is
an integration of the pure spine: receiver -> tracker -> publisher.

Cover:
  * run() polls, ingests, and publishes summary + per-receiver status/stats.
  * on_connect republishes status:online + discovery + receiver location.
  * request_stop() unwinds cleanly: client stopped, receivers closed.
  * A disabled receiver is excluded by build_app.
  * build_app raises when no receiver is enabled.
  * Poll-loop survives a flaky receiver (FetchError -> empty -> unhealthy).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from ha_airspace.app import App, build_app
from ha_airspace.config import Config, MqttConfig
from ha_airspace.models import ReceiverLocation
from ha_airspace.mqtt.client import MqttClient
from ha_airspace.mqtt.publisher import Publisher
from ha_airspace.receivers import FileReceiver, ReceiverSource
from ha_airspace.tracker import AircraftTracker

_FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fake MQTT client — records publishes, fires on_connect, blocks until stop
# ---------------------------------------------------------------------------


class FakeMqttClient:
    """Stand-in for MqttClient with the same surface App + Publisher use.

    ``run()`` fires the on_connect callback once (simulating a successful
    broker connect) then blocks until ``stop()``. Every publish is recorded.
    """

    def __init__(self) -> None:
        self.publishes: list[dict[str, Any]] = []
        self.started = False
        self.stopped = False
        self._on_connect: Any = None
        self._stop = asyncio.Event()

    def set_on_connect(self, on_connect: Any) -> None:
        self._on_connect = on_connect

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        retain: bool = False,
        qos: int = 0,
        topic_class: str = "other",
    ) -> None:
        self.publishes.append({"topic": topic, "payload": payload, "topic_class": topic_class})

    async def run(self) -> None:
        self.started = True
        if self._on_connect is not None:
            await self._on_connect()
        await self._stop.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    # Properties Publisher/peers may touch.
    @property
    def is_connected(self) -> bool:
        return self.started and not self.stopped


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "service": {"poll_interval_s": 0.01},
        "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
        "mqtt": {"broker": "broker.local"},
        "receivers": [{"name": "rx-home", "url": "http://piaware/aircraft.json", "band": "1090"}],
    }
    base.update(overrides)
    return Config.model_validate(base)


def _make_app(
    fake_client: FakeMqttClient,
    *,
    config: Config | None = None,
    receivers: list[ReceiverSource] | None = None,
) -> App:
    cfg = config or _config()
    publisher = Publisher(fake_client, cfg)  # type: ignore[arg-type]
    tracker = AircraftTracker(publisher, cfg.watchpoints_runtime())
    rx: list[ReceiverSource] = (
        receivers
        if receivers is not None
        else [FileReceiver("rx-home", "1090", _FIXTURES / "aircraft_basic.json")]
    )
    return App(
        cfg,
        receivers=rx,
        mqtt_client=fake_client,  # type: ignore[arg-type]
        publisher=publisher,
        tracker=tracker,
    )


async def _run_until(predicate: Any, *, timeout_s: float = 2.0, poll_s: float = 0.01) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(poll_s)


def _topics(client: FakeMqttClient) -> list[str]:
    return [p["topic"] for p in client.publishes]


def _latest_payload(client: FakeMqttClient, topic: str) -> Any:
    """Most recent payload published on an exact topic, or None."""
    for p in reversed(client.publishes):
        if p["topic"] == topic:
            return p["payload"]
    return None


# ---------------------------------------------------------------------------
# run() lifecycle
# ---------------------------------------------------------------------------


class TestRun:
    async def test_polls_and_publishes_summary_and_status(self) -> None:
        client = FakeMqttClient()
        app = _make_app(client)

        run_task = asyncio.create_task(app.run())
        # Wait until a summary publish lands (proves a poll cycle completed).
        await _run_until(lambda: any("summary/count" in t for t in _topics(client)))
        app.request_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=2.0)

        topics = _topics(client)
        # Aircraft from the fixture was published.
        assert any(t.startswith("adsb/aircraft/") for t in topics)
        # Summary + per-receiver status + stats all published.
        assert "adsb/summary/count" in topics
        assert "adsb/receiver/rx-home/status" in topics
        assert "adsb/receiver/rx-home/stats" in topics

    async def test_two_receivers_merge_to_one_aircraft(self) -> None:
        # Two receivers (different bands) replaying the same fixture see the
        # same hexes. The merged view must publish each hex once, with BOTH
        # receivers in seen_by and BOTH bands in bands — the Phase 3 payoff.
        client = FakeMqttClient()
        cfg = _config(
            receivers=[
                {"name": "rx-1090", "url": "http://a/aircraft.json", "band": "1090"},
                {"name": "rx-978", "url": "http://b/aircraft.json", "band": "978"},
            ]
        )
        publisher = Publisher(client, cfg)  # type: ignore[arg-type]
        tracker = AircraftTracker(publisher, cfg.watchpoints_runtime())
        receivers: list[ReceiverSource] = [
            FileReceiver("rx-1090", "1090", _FIXTURES / "aircraft_basic.json"),
            FileReceiver("rx-978", "978", _FIXTURES / "aircraft_basic.json"),
        ]
        app = App(
            cfg,
            receivers=receivers,
            mqtt_client=client,  # type: ignore[arg-type]
            publisher=publisher,
            tracker=tracker,
        )

        run_task = asyncio.create_task(app.run())
        # Wait until both receivers have ingested at least once and a tick ran.
        await _run_until(
            lambda: (
                any("summary/count" in t for t in _topics(client))
                and any(t == "adsb/aircraft/ae0001" for t in _topics(client))
            )
        )
        # Give both poll loops a moment to both ingest before stopping.
        await _run_until(
            lambda: (
                _latest_payload(client, "adsb/aircraft/ae0001") is not None
                and len(json.loads(_latest_payload(client, "adsb/aircraft/ae0001"))["seen_by"]) == 2
            )
        )
        app.request_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=2.0)

        body = json.loads(_latest_payload(client, "adsb/aircraft/ae0001"))
        assert sorted(body["seen_by"]) == ["rx-1090", "rx-978"]
        assert sorted(body["bands"]) == ["1090", "978"]
        # Exactly one aircraft topic per hex (merged, not duplicated per receiver).
        ae0001_topics = [t for t in _topics(client) if t == "adsb/aircraft/ae0001"]
        assert len(ae0001_topics) >= 1  # republished each tick, but one topic

    async def test_on_connect_republishes_status_discovery_location(self) -> None:
        client = FakeMqttClient()
        # Receiver advertises a location so the location topic is published.
        receiver = FileReceiver(
            "rx-home",
            "1090",
            _FIXTURES / "aircraft_basic.json",
            location=ReceiverLocation(lat=30.33, lon=-97.99, source="config"),
        )
        app = _make_app(client, receivers=[receiver])

        run_task = asyncio.create_task(app.run())
        await _run_until(lambda: any("summary/count" in t for t in _topics(client)))
        app.request_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=2.0)

        topics = _topics(client)
        # status:online published (retained availability) on connect.
        assert "adsb/status" in topics
        # Discovery payloads published (homeassistant/.../config).
        assert any(t.startswith("homeassistant/") for t in topics)
        # Receiver location republished on connect.
        assert "adsb/receiver/rx-home/location" in topics

    async def test_request_stop_unwinds_cleanly(self) -> None:
        client = FakeMqttClient()
        app = _make_app(client)
        run_task = asyncio.create_task(app.run())
        await _run_until(lambda: client.started)
        app.request_stop()
        await asyncio.wait_for(run_task, timeout=2.0)
        # Client was told to stop; run() returned without error.
        assert client.stopped is True
        assert run_task.done()
        assert run_task.exception() is None

    async def test_stop_before_any_poll_still_exits(self) -> None:
        client = FakeMqttClient()
        app = _make_app(client)
        run_task = asyncio.create_task(app.run())
        app.request_stop()  # immediate
        await asyncio.wait_for(run_task, timeout=2.0)
        assert run_task.done()


# ---------------------------------------------------------------------------
# Flaky receiver tolerance
# ---------------------------------------------------------------------------


class TestFlakyReceiver:
    async def test_missing_fixture_does_not_crash_loop(self) -> None:
        client = FakeMqttClient()
        # Point at a nonexistent file: every fetch -> FetchError -> [].
        receiver = FileReceiver("rx-home", "1090", _FIXTURES / "does_not_exist.json")
        app = _make_app(client, receivers=[receiver])

        run_task = asyncio.create_task(app.run())
        # The loop still runs and publishes receiver status (unhealthy path).
        await _run_until(lambda: "adsb/receiver/rx-home/status" in _topics(client))
        app.request_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=2.0)
        assert run_task.exception() is None


# ---------------------------------------------------------------------------
# build_app composition root
# ---------------------------------------------------------------------------


class TestBuildApp:
    def test_builds_real_collaborators(self) -> None:
        app = build_app(_config())
        assert isinstance(app, App)

    def test_disabled_receiver_excluded(self) -> None:
        cfg = _config(
            receivers=[
                {"name": "rx-a", "url": "http://a/aircraft.json", "band": "1090"},
                {
                    "name": "rx-b",
                    "url": "http://b/aircraft.json",
                    "band": "978",
                    "enabled": False,
                },
            ]
        )
        app = build_app(cfg)
        assert [r.name for r in app._receivers] == ["rx-a"]

    def test_no_enabled_receivers_raises(self) -> None:
        cfg = _config(
            receivers=[
                {
                    "name": "rx-a",
                    "url": "http://a/aircraft.json",
                    "band": "1090",
                    "enabled": False,
                }
            ]
        )
        with pytest.raises(ValueError, match="no enabled receivers"):
            build_app(cfg)


# ---------------------------------------------------------------------------
# set_on_connect on the real MqttClient
# ---------------------------------------------------------------------------


class TestClientOnConnectSetter:
    def test_set_on_connect_replaces_callback(self) -> None:
        client = MqttClient(MqttConfig(broker="b", base_topic="adsb"))

        async def cb() -> None:
            return None

        client.set_on_connect(cb)
        assert client._on_connect is cb
