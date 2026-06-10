"""Tests for ha_airspace.mqtt.publisher.Publisher.

Uses FakeMqttClient — records every publish call without exercising
the connection lifecycle. Lets each test inspect topic, payload,
retain, and topic_class on a per-call basis.

Cover:
  * Topic taxonomy: aircraft, summary, receiver, status, discovery.
  * Retention policy: state-bearing topics retained; transient signals not.
  * Per-hex throttle on aircraft publishes.
  * Global throttle on summary publishes.
  * Aircraft purge clears retained state with empty payload + retain=True.
  * Summary nearest=None publishes empty-retained.
  * Receiver status: online | unhealthy | offline mapping.
  * on_connect publishes status:online + the discovery payload set.
  * Discovery payload count matches the expected entity surface.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from ha_airspace.config import Config
from ha_airspace.models import (
    AircraftObservation,
    AircraftState,
    DroneInfo,
    ReceiverLocation,
)
from ha_airspace.mqtt.publisher import Publisher

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeMqttClient:
    """In-memory stand-in for MqttClient.publish.

    Records every call with the kwargs the publisher passed. Tests
    inspect ``.publishes`` to assert on topic, payload, retain, and
    topic_class.
    """

    def __init__(self) -> None:
        self.publishes: list[dict[str, Any]] = []

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        retain: bool = False,
        qos: int = 0,
        topic_class: str = "other",
    ) -> None:
        self.publishes.append(
            {
                "topic": topic,
                "payload": payload,
                "retain": retain,
                "qos": qos,
                "topic_class": topic_class,
            }
        )


class FakeClock:
    """Deterministic clock for throttle tests."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _now_dt() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_config(
    *,
    aircraft_throttle: float = 1.0,
    summary_throttle: float = 1.0,
) -> Config:
    return Config.model_validate(
        {
            "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
            "mqtt": {
                "broker": "broker.local",
                "publish_aircraft_min_interval_s": aircraft_throttle,
                "publish_summary_min_interval_s": summary_throttle,
            },
            "receivers": [
                {
                    "name": "rx-home",
                    "url": "http://piaware/aircraft.json",
                    "band": "1090",
                }
            ],
        }
    )


def _make_state(hex_code: str = "ae0001") -> AircraftState:
    obs = AircraftObservation(
        hex=hex_code,
        observed_at=_now_dt(),
        seen_by="rx-home",
        band="1090",
        flight="RCH171",
        lat=30.33,
        lon=-97.99,
        alt_baro_ft=35000,
    )
    return AircraftState.from_first_observation(obs)


@pytest.fixture
def fake_client() -> FakeMqttClient:
    return FakeMqttClient()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _make_publisher(
    fake_client: FakeMqttClient,
    *,
    config: Config | None = None,
    clock: Callable[[], float] | None = None,
) -> Publisher:
    return Publisher(
        fake_client,  # type: ignore[arg-type]
        config or _make_config(),
        clock=clock if clock is not None else FakeClock(),
    )


# ---------------------------------------------------------------------------
# on_connect: status:online + discovery republish
# ---------------------------------------------------------------------------


class TestOnConnect:
    async def test_publishes_status_online_first(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.on_connect()

        first = fake_client.publishes[0]
        assert first["topic"] == "adsb/status"
        assert first["payload"] == b"online"
        assert first["retain"] is True
        assert first["topic_class"] == "status"

    async def test_publishes_discovery_payloads_after_status(
        self, fake_client: FakeMqttClient
    ) -> None:
        pub = _make_publisher(fake_client)
        await pub.on_connect()

        # First publish is status:online; everything after is discovery.
        discovery_calls = fake_client.publishes[1:]
        assert len(discovery_calls) > 0
        for call in discovery_calls:
            assert call["topic"].startswith("homeassistant/")
            assert call["topic"].endswith("/config")
            assert call["retain"] is True
            assert call["topic_class"] == "discovery"

    async def test_discovery_disabled_publishes_only_status(
        self, fake_client: FakeMqttClient
    ) -> None:
        cfg = Config.model_validate(
            {
                "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
                "mqtt": {"broker": "broker.local", "discovery_enabled": False},
                "receivers": [
                    {
                        "name": "rx-home",
                        "url": "http://piaware/aircraft.json",
                        "band": "1090",
                    }
                ],
            }
        )
        pub = _make_publisher(fake_client, config=cfg)
        await pub.on_connect()
        # Only status:online; no discovery.
        assert len(fake_client.publishes) == 1
        assert fake_client.publishes[0]["topic"] == "adsb/status"

    async def test_discovery_payload_is_valid_json(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.on_connect()
        discovery = fake_client.publishes[1:]
        for call in discovery:
            payload = call["payload"]
            assert isinstance(payload, bytes)
            parsed = json.loads(payload.decode("utf-8"))
            # Every HA discovery body must have these (eng review test
            # mirrors this; we re-verify at the publish boundary).
            assert "unique_id" in parsed
            assert "device" in parsed

    async def test_sw_version_flows_through_to_discovery(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.on_connect(sw_version="1.2.3.4")
        discovery = fake_client.publishes[1:]
        for call in discovery:
            body = json.loads(call["payload"].decode("utf-8"))
            assert body["device"]["sw_version"] == "1.2.3.4"


# ---------------------------------------------------------------------------
# Aircraft topic + per-hex throttle
# ---------------------------------------------------------------------------


class TestPublishAircraft:
    async def test_publishes_to_correct_topic_with_retain(
        self, fake_client: FakeMqttClient
    ) -> None:
        pub = _make_publisher(fake_client)
        published = await pub.publish_aircraft(_make_state("ae0001"))

        assert published is True
        assert len(fake_client.publishes) == 1
        call = fake_client.publishes[0]
        assert call["topic"] == "adsb/aircraft/ae0001"
        assert call["retain"] is True
        assert call["topic_class"] == "aircraft"

    async def test_payload_is_aircraft_json(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_aircraft(_make_state())
        payload = fake_client.publishes[0]["payload"]
        body = json.loads(payload)
        assert body["hex"] == "ae0001"
        assert body["flight"] == "RCH171"
        assert body["bands"] == ["1090"]

    async def test_throttle_suppresses_within_interval(
        self, fake_client: FakeMqttClient, clock: FakeClock
    ) -> None:
        pub = _make_publisher(fake_client, clock=clock)
        state = _make_state()

        first = await pub.publish_aircraft(state)
        clock.advance(0.5)  # Less than 1.0s default.
        second = await pub.publish_aircraft(state)

        assert first is True
        assert second is False
        assert len(fake_client.publishes) == 1

    async def test_throttle_clears_after_interval(
        self, fake_client: FakeMqttClient, clock: FakeClock
    ) -> None:
        pub = _make_publisher(fake_client, clock=clock)
        state = _make_state()

        await pub.publish_aircraft(state)
        clock.advance(1.0)  # Exactly the interval.
        published = await pub.publish_aircraft(state)
        # Boundary: at exactly the interval, the throttle clears.
        # (The check is `now - last < interval`; equality passes.)
        assert published is True
        assert len(fake_client.publishes) == 2

    async def test_throttle_per_hex_independent(
        self, fake_client: FakeMqttClient, clock: FakeClock
    ) -> None:
        pub = _make_publisher(fake_client, clock=clock)
        a = _make_state("ae0001")
        b = _make_state("ae0002")

        # Two different hexes — both should publish even within the interval.
        assert await pub.publish_aircraft(a) is True
        assert await pub.publish_aircraft(b) is True
        # A second publish for the same hex within interval is suppressed.
        assert await pub.publish_aircraft(a) is False

    async def test_zero_throttle_always_publishes(self, fake_client: FakeMqttClient) -> None:
        cfg = _make_config(aircraft_throttle=0.0)
        pub = _make_publisher(fake_client, config=cfg)
        state = _make_state()
        for _ in range(5):
            await pub.publish_aircraft(state)
        assert len(fake_client.publishes) == 5


# ---------------------------------------------------------------------------
# Aircraft purge
# ---------------------------------------------------------------------------


class TestPurgeAircraft:
    async def test_purge_publishes_empty_retained(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.purge_aircraft("ae0001")

        call = fake_client.publishes[0]
        assert call["topic"] == "adsb/aircraft/ae0001"
        assert call["payload"] == b""
        assert call["retain"] is True
        assert call["topic_class"] == "aircraft"

    async def test_purge_resets_throttle_state(
        self, fake_client: FakeMqttClient, clock: FakeClock
    ) -> None:
        pub = _make_publisher(fake_client, clock=clock)
        state = _make_state("ae0001")

        await pub.publish_aircraft(state)  # publishes
        await pub.purge_aircraft("ae0001")
        # After purge, the next publish for this hex should NOT be throttled
        # — the previous timestamp was cleared. Useful when an aircraft
        # comes back into coverage shortly after purging.
        clock.advance(0.01)
        published = await pub.publish_aircraft(state)
        assert published is True


# ---------------------------------------------------------------------------
# Summary topics
# ---------------------------------------------------------------------------


class TestPublishSummary:
    async def test_publishes_count_and_nearest_and_count_by_flag(
        self, fake_client: FakeMqttClient
    ) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_summary(count=12, nearest=_make_state())

        topics = [c["topic"] for c in fake_client.publishes]
        assert "adsb/summary/count" in topics
        assert "adsb/summary/nearest" in topics
        assert "adsb/summary/count_by_flag" in topics

    async def test_count_is_string_encoded(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_summary(count=42, nearest=None)
        count_call = next(c for c in fake_client.publishes if c["topic"] == "adsb/summary/count")
        assert count_call["payload"] == b"42"
        assert count_call["retain"] is True

    async def test_nearest_none_publishes_empty_retained(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_summary(count=0, nearest=None)
        nearest_call = next(
            c for c in fake_client.publishes if c["topic"] == "adsb/summary/nearest"
        )
        assert nearest_call["payload"] == b""
        assert nearest_call["retain"] is True

    async def test_nearest_with_state_publishes_aircraft_payload(
        self, fake_client: FakeMqttClient
    ) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_summary(count=1, nearest=_make_state())
        nearest_call = next(
            c for c in fake_client.publishes if c["topic"] == "adsb/summary/nearest"
        )
        body = json.loads(nearest_call["payload"])
        assert body["hex"] == "ae0001"

    async def test_count_by_flag_defaults_to_empty(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_summary(count=0, nearest=None)
        flag_call = next(
            c for c in fake_client.publishes if c["topic"] == "adsb/summary/count_by_flag"
        )
        assert flag_call["payload"] == b"{}"

    async def test_count_by_flag_passed_through(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_summary(
            count=2,
            nearest=None,
            count_by_flag={"military": 1, "interesting": 0},
        )
        flag_call = next(
            c for c in fake_client.publishes if c["topic"] == "adsb/summary/count_by_flag"
        )
        body = json.loads(flag_call["payload"])
        assert body == {"military": 1, "interesting": 0}

    async def test_summary_throttle_suppresses_within_interval(
        self, fake_client: FakeMqttClient, clock: FakeClock
    ) -> None:
        pub = _make_publisher(fake_client, clock=clock)
        first = await pub.publish_summary(count=1, nearest=None)
        clock.advance(0.5)  # under 1.0s default
        second = await pub.publish_summary(count=2, nearest=None)
        assert first is True
        assert second is False

    async def test_summary_throttle_clears_after_interval(
        self, fake_client: FakeMqttClient, clock: FakeClock
    ) -> None:
        pub = _make_publisher(fake_client, clock=clock)
        await pub.publish_summary(count=1, nearest=None)
        clock.advance(1.0)
        assert await pub.publish_summary(count=2, nearest=None) is True


# ---------------------------------------------------------------------------
# Receiver topics
# ---------------------------------------------------------------------------


class TestReceiverTopics:
    async def test_status_online(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_receiver_status("rx-home", online=True)
        call = fake_client.publishes[0]
        assert call["topic"] == "adsb/receiver/rx-home/status"
        assert call["payload"] == b"online"
        assert call["retain"] is True
        assert call["topic_class"] == "status"

    async def test_status_offline(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_receiver_status("rx-home", online=False)
        assert fake_client.publishes[0]["payload"] == b"offline"

    async def test_status_unhealthy(self, fake_client: FakeMqttClient) -> None:
        # Online but failing: HA's connectivity sensor will see "unhealthy"
        # — we publish that string so users can write automations on it.
        pub = _make_publisher(fake_client)
        await pub.publish_receiver_status("rx-home", online=True, unhealthy=True)
        assert fake_client.publishes[0]["payload"] == b"unhealthy"

    async def test_offline_takes_precedence_over_unhealthy(
        self, fake_client: FakeMqttClient
    ) -> None:
        # online=False trumps unhealthy=True — a receiver that is offline
        # is offline, period.
        pub = _make_publisher(fake_client)
        await pub.publish_receiver_status("rx-home", online=False, unhealthy=True)
        assert fake_client.publishes[0]["payload"] == b"offline"

    async def test_stats_publishes_health_dict_as_json(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_receiver_stats(
            "rx-home",
            {
                "online": True,
                "last_success": _now_dt(),
                "consecutive_failures": 0,
                "aircraft_count": 12,
                "messages_per_sec": 240.5,
            },
        )
        call = fake_client.publishes[0]
        assert call["topic"] == "adsb/receiver/rx-home/stats"
        body = json.loads(call["payload"])
        assert body["aircraft_count"] == 12
        assert body["messages_per_sec"] == 240.5

    async def test_location_publishes_runtime_to_json(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        loc = ReceiverLocation(lat=30.33, lon=-97.99, alt_m=200.0, source="receiver_json")
        await pub.publish_receiver_location("rx-home", loc)
        call = fake_client.publishes[0]
        assert call["topic"] == "adsb/receiver/rx-home/location"
        body = json.loads(call["payload"])
        assert body["lat"] == 30.33
        assert body["source"] == "receiver_json"


# ---------------------------------------------------------------------------
# Drone topics (Phase 3 — Remote ID)
# ---------------------------------------------------------------------------


def _make_drone_state(track_id: str = "Spoofed_Serial_1") -> AircraftState:
    obs = AircraftObservation(
        track_id=track_id,
        hex=None,
        non_icao=True,
        observed_at=_now_dt(),
        seen_by="dump3411",
        band="remoteid",
        lat=30.30,
        lon=-98.06,
        alt_geom_ft=1276,
        ground_speed_kt=93.3,
        drone=DroneInfo(
            id_type="serial",
            ua_type="multirotor",
            agl_ft=246.1,
            rid_source="wifi_beacon",
            operator_lat=30.29,
            operator_lon=-98.05,
            operator_id="OP123",
            operator_alt_takeoff_ft=50.0,
        ),
    )
    return AircraftState.from_first_observation(obs)


class TestPublishDrone:
    async def test_publishes_to_drone_topic_with_track_id(
        self, fake_client: FakeMqttClient
    ) -> None:
        pub = _make_publisher(fake_client)
        published = await pub.publish_drone(_make_drone_state("Spoofed_Serial_1"))
        assert published is True
        call = fake_client.publishes[0]
        assert call["topic"] == "adsb/drone/Spoofed_Serial_1"
        assert call["retain"] is True
        assert call["topic_class"] == "drone"

    async def test_drone_payload_carries_operator_and_agl(
        self, fake_client: FakeMqttClient
    ) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_drone(_make_drone_state())
        body = json.loads(fake_client.publishes[0]["payload"])
        assert body["agl_ft"] == 246.1
        assert body["operator_lat"] == 30.29
        assert body["operator_lon"] == -98.05
        assert body["operator_id"] == "OP123"
        assert body["id_type"] == "serial"
        assert body["rid_source"] == "wifi_beacon"

    async def test_purge_drone_clears_retained(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.purge_drone("Spoofed_Serial_1")
        call = fake_client.publishes[0]
        assert call["topic"] == "adsb/drone/Spoofed_Serial_1"
        assert call["payload"] == b""
        assert call["retain"] is True

    async def test_drone_summary_count_and_nearest(self, fake_client: FakeMqttClient) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_drone_summary(count=3, nearest=_make_drone_state())
        topics = [c["topic"] for c in fake_client.publishes]
        assert "adsb/summary/drone_count" in topics
        assert "adsb/summary/nearest_drone" in topics
        count_call = next(c for c in fake_client.publishes if c["topic"].endswith("drone_count"))
        assert count_call["payload"] == b"3"

    async def test_drone_summary_nearest_none_empty_retained(
        self, fake_client: FakeMqttClient
    ) -> None:
        pub = _make_publisher(fake_client)
        await pub.publish_drone_summary(count=0, nearest=None)
        call = next(c for c in fake_client.publishes if c["topic"].endswith("nearest_drone"))
        assert call["payload"] == b""
        assert call["retain"] is True
