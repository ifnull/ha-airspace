"""Tests for ha_airspace.mqtt.discovery.

Cover:
  * Topic format conforms to HA's discovery convention.
  * Every entity has the required keys (unique_id, state_topic, device).
  * NO per-aircraft entities — load-bearing test, this is the contract
    that protects HA's entity registry.
  * Per-receiver entities reference the right state topics.
  * Disabled receivers don't get entities.
  * discovery_enabled: false short-circuits to no payloads.
  * Custom discovery_prefix and base_topic flow through.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_airspace.config import Config
from ha_airspace.mqtt.discovery import build_discovery_payloads


def _make_config(**mqtt_overrides: object) -> Config:
    base: dict[str, Any] = {
        "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
        "mqtt": {
            "broker": "broker.local",
            **mqtt_overrides,
        },
        "receivers": [
            {
                "name": "rx-home",
                "url": "http://piaware/aircraft.json",
                "band": "1090",
            }
        ],
    }
    return Config.model_validate(base)


def _make_config_multi_receiver() -> Config:
    return Config.model_validate(
        {
            "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
            "mqtt": {"broker": "broker.local"},
            "receivers": [
                {
                    "name": "rx-1090",
                    "url": "http://a/aircraft.json",
                    "band": "1090",
                },
                {
                    "name": "rx-978",
                    "url": "http://b/aircraft.json",
                    "band": "978",
                },
            ],
        }
    )


def _topics(payloads: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [topic for topic, _ in payloads]


def _bodies_by_unique_id(
    payloads: list[tuple[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    return {body["unique_id"]: body for _, body in payloads}


# ---------------------------------------------------------------------------
# Top-level surface
# ---------------------------------------------------------------------------


class TestSurface:
    def test_returns_empty_when_discovery_disabled(self) -> None:
        cfg = _make_config(discovery_enabled=False)
        assert build_discovery_payloads(cfg) == []

    def test_returns_payloads_when_discovery_enabled(self) -> None:
        cfg = _make_config()  # discovery_enabled defaults to True
        payloads = build_discovery_payloads(cfg)
        assert len(payloads) > 0


# ---------------------------------------------------------------------------
# Anti-goal: NO per-aircraft entities. Load-bearing test.
# ---------------------------------------------------------------------------


class TestNoPerAircraftEntities:
    def test_no_payload_targets_aircraft_topic(self) -> None:
        # The wildcard topic adsb/aircraft/<hex> must NEVER appear in
        # any discovery payload's state_topic. This is the protection
        # against blowing up HA's entity registry.
        cfg = _make_config()
        payloads = build_discovery_payloads(cfg)
        for _, body in payloads:
            state_topic = body.get("state_topic", "")
            assert "/aircraft/" not in state_topic, (
                f"discovery payload references per-aircraft topic: "
                f"{state_topic} in {body['unique_id']}"
            )

    def test_no_payload_targets_aircraft_wildcard(self) -> None:
        # Same protection from a different angle: no payload uses an
        # MQTT wildcard, and no JSON-attributes topic points at the
        # per-aircraft surface either.
        cfg = _make_config()
        payloads = build_discovery_payloads(cfg)
        for _, body in payloads:
            for key in ("state_topic", "json_attributes_topic"):
                topic = body.get(key, "")
                assert "+" not in topic
                assert "#" not in topic
                assert "/aircraft/" not in topic


# ---------------------------------------------------------------------------
# Service-wide entities
# ---------------------------------------------------------------------------


class TestSummaryEntities:
    def test_count_entity_present(self) -> None:
        payloads = build_discovery_payloads(_make_config())
        bodies = _bodies_by_unique_id(payloads)
        assert "adsb_count" in bodies
        body = bodies["adsb_count"]
        assert body["state_topic"] == "adsb/summary/count"

    def test_nearest_entity_present(self) -> None:
        payloads = build_discovery_payloads(_make_config())
        bodies = _bodies_by_unique_id(payloads)
        assert "adsb_nearest" in bodies
        body = bodies["adsb_nearest"]
        assert body["state_topic"] == "adsb/summary/nearest"
        # Attributes topic should match — HA pulls full aircraft JSON
        # from there.
        assert body["json_attributes_topic"] == "adsb/summary/nearest"

    def test_summary_entities_have_availability_topic(self) -> None:
        # HA marks entity unavailable when service goes offline (LWT).
        payloads = build_discovery_payloads(_make_config())
        bodies = _bodies_by_unique_id(payloads)
        for unique_id in ("adsb_count", "adsb_nearest"):
            body = bodies[unique_id]
            assert body["availability_topic"] == "adsb/status"
            assert body["payload_available"] == "online"
            assert body["payload_not_available"] == "offline"


# ---------------------------------------------------------------------------
# Per-receiver entities
# ---------------------------------------------------------------------------


class TestReceiverEntities:
    def test_three_entities_per_receiver(self) -> None:
        payloads = build_discovery_payloads(_make_config())
        bodies = _bodies_by_unique_id(payloads)
        assert "adsb_receiver_rx-home_status" in bodies
        assert "adsb_receiver_rx-home_aircraft_count" in bodies
        assert "adsb_receiver_rx-home_messages_per_sec" in bodies

    def test_status_entity_topic_and_class(self) -> None:
        payloads = build_discovery_payloads(_make_config())
        body = _bodies_by_unique_id(payloads)["adsb_receiver_rx-home_status"]
        assert body["state_topic"] == "adsb/receiver/rx-home/status"
        assert body["device_class"] == "connectivity"
        assert body["payload_on"] == "online"
        assert body["payload_off"] == "offline"

    def test_stats_entities_use_value_template(self) -> None:
        # Both stats sensors read from one stats topic + use
        # value_template to extract their field. Avoids duplicate
        # publishes per metric.
        payloads = build_discovery_payloads(_make_config())
        bodies = _bodies_by_unique_id(payloads)

        ac = bodies["adsb_receiver_rx-home_aircraft_count"]
        assert ac["state_topic"] == "adsb/receiver/rx-home/stats"
        assert ac["value_template"] == "{{ value_json.aircraft_count }}"

        mps = bodies["adsb_receiver_rx-home_messages_per_sec"]
        assert mps["state_topic"] == "adsb/receiver/rx-home/stats"
        assert mps["value_template"] == "{{ value_json.messages_per_sec }}"
        assert mps["unit_of_measurement"] == "msg/s"

    def test_multi_receiver_emits_three_entities_each(self) -> None:
        cfg = _make_config_multi_receiver()
        payloads = build_discovery_payloads(cfg)
        bodies = _bodies_by_unique_id(payloads)

        # Two receivers x 3 entities = 6 receiver entities,
        # plus 2 service-wide = 8 total.
        for receiver_name in ("rx-1090", "rx-978"):
            for entity_kind in (
                "status",
                "aircraft_count",
                "messages_per_sec",
            ):
                unique_id = f"adsb_receiver_{receiver_name}_{entity_kind}"
                assert unique_id in bodies, f"missing {unique_id}"

        assert len(payloads) == 2 + 6  # service-wide + per-receiver

    def test_disabled_receiver_emits_no_entities(self) -> None:
        cfg = Config.model_validate(
            {
                "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
                "mqtt": {"broker": "broker.local"},
                "receivers": [
                    {
                        "name": "rx-home",
                        "url": "http://piaware/aircraft.json",
                        "band": "1090",
                        "enabled": False,
                    },
                    {
                        "name": "rx-other",
                        "url": "http://other/aircraft.json",
                        "band": "1090",
                    },
                ],
            }
        )
        payloads = build_discovery_payloads(cfg)
        bodies = _bodies_by_unique_id(payloads)
        # rx-home is disabled — no discovery for it.
        for kind in ("status", "aircraft_count", "messages_per_sec"):
            assert f"adsb_receiver_rx-home_{kind}" not in bodies
        # rx-other is enabled — should be present.
        for kind in ("status", "aircraft_count", "messages_per_sec"):
            assert f"adsb_receiver_rx-other_{kind}" in bodies


# ---------------------------------------------------------------------------
# Topic format
# ---------------------------------------------------------------------------


class TestTopicFormat:
    def test_topic_uses_discovery_prefix_and_node_id(self) -> None:
        payloads = build_discovery_payloads(_make_config())
        for topic, _body in payloads:
            # Format: <prefix>/<component>/adsb/<object_id>/config
            parts = topic.split("/")
            assert len(parts) == 5, f"unexpected topic shape: {topic}"
            assert parts[0] == "homeassistant"  # default discovery_prefix
            assert parts[1] in ("sensor", "binary_sensor")
            assert parts[2] == "adsb"
            assert parts[4] == "config"

    def test_custom_discovery_prefix_flows_through(self) -> None:
        cfg = _make_config(discovery_prefix="myha")
        payloads = build_discovery_payloads(cfg)
        for topic, _ in payloads:
            assert topic.startswith("myha/")

    def test_custom_base_topic_flows_through(self) -> None:
        cfg = _make_config(base_topic="planes")
        payloads = build_discovery_payloads(cfg)
        bodies = _bodies_by_unique_id(payloads)
        # State topics should use the new base.
        assert bodies["adsb_count"]["state_topic"] == "planes/summary/count"
        assert (
            bodies["adsb_receiver_rx-home_status"]["state_topic"]
            == "planes/receiver/rx-home/status"
        )
        # Availability topic too.
        for body in bodies.values():
            assert body["availability_topic"] == "planes/status"


# ---------------------------------------------------------------------------
# Device block — entities should group under one device
# ---------------------------------------------------------------------------


class TestDeviceBlock:
    def test_every_entity_has_device_block(self) -> None:
        payloads = build_discovery_payloads(_make_config())
        for _, body in payloads:
            assert "device" in body
            device = body["device"]
            assert "identifiers" in device
            assert "name" in device

    def test_all_entities_share_device_identifier(self) -> None:
        # Single device card in HA grouping every adsb entity.
        payloads = build_discovery_payloads(_make_config())
        ids = {tuple(body["device"]["identifiers"]) for _, body in payloads}
        assert ids == {("ha_airspace",)}

    def test_sw_version_included_when_provided(self) -> None:
        payloads = build_discovery_payloads(_make_config(), sw_version="0.1.2.3")
        for _, body in payloads:
            assert body["device"]["sw_version"] == "0.1.2.3"

    def test_sw_version_omitted_when_none(self) -> None:
        payloads = build_discovery_payloads(_make_config())
        for _, body in payloads:
            assert "sw_version" not in body["device"]


# ---------------------------------------------------------------------------
# Required HA-discovery field invariants
# ---------------------------------------------------------------------------


class TestRequiredFields:
    @pytest.mark.parametrize("field", ["unique_id", "state_topic", "name", "device"])
    def test_every_entity_has_field(self, field: str) -> None:
        payloads = build_discovery_payloads(_make_config())
        for _, body in payloads:
            assert field in body, f"{field!r} missing from {body['unique_id']}"

    def test_unique_ids_are_unique(self) -> None:
        # If two entities collide on unique_id HA shows only one.
        payloads = build_discovery_payloads(_make_config_multi_receiver())
        ids = [body["unique_id"] for _, body in payloads]
        assert len(ids) == len(set(ids)), f"duplicate unique_ids: {ids}"


# ---------------------------------------------------------------------------
# Per-alert-rule binary sensors (slice 3)
# ---------------------------------------------------------------------------


class TestAlertEntities:
    def _config_with_alerts(self) -> Config:
        return Config.model_validate(
            {
                "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
                "mqtt": {"broker": "broker.local"},
                "receivers": [
                    {"name": "rx-home", "url": "http://piaware/aircraft.json", "band": "1090"}
                ],
                "enrichment": {
                    "alerts": {
                        "rules": [
                            {"name": "military_close", "match": {"flags": ["military"]}},
                            {"name": "emergency", "match": {"flags": ["emergency"]}},
                        ]
                    }
                },
            }
        )

    def test_binary_sensor_per_rule(self) -> None:
        payloads = build_discovery_payloads(self._config_with_alerts())
        bodies = _bodies_by_unique_id(payloads)
        assert "adsb_alert_military_close" in bodies
        assert "adsb_alert_emergency" in bodies

    def test_alert_sensor_shape(self) -> None:
        payloads = build_discovery_payloads(self._config_with_alerts())
        body = _bodies_by_unique_id(payloads)["adsb_alert_military_close"]
        assert body["state_topic"] == "adsb/alert/military_close/active"
        assert body["payload_on"] == "on"
        assert body["payload_off"] == "off"
        assert body["device_class"] == "safety"

    def test_no_alert_entities_without_rules(self) -> None:
        payloads = build_discovery_payloads(_make_config())
        assert not any("alert" in t for t in _topics(payloads))


# ---------------------------------------------------------------------------
# Drone entities (Phase 3 — Remote ID)
# ---------------------------------------------------------------------------


class TestDroneEntities:
    def _config_with_drones(self) -> Config:
        return Config.model_validate(
            {
                "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
                "mqtt": {"broker": "broker.local"},
                "receivers": [
                    {"name": "rx-home", "url": "http://piaware/aircraft.json", "band": "1090"}
                ],
                "remoteid": [
                    {"name": "dump3411", "url": "http://drone.local:8754/data/remoteid.json"}
                ],
            }
        )

    def test_drone_entities_present(self) -> None:
        bodies = _bodies_by_unique_id(build_discovery_payloads(self._config_with_drones()))
        assert "adsb_drone_count" in bodies
        assert "adsb_nearest_drone" in bodies

    def test_drone_count_state_topic(self) -> None:
        bodies = _bodies_by_unique_id(build_discovery_payloads(self._config_with_drones()))
        assert bodies["adsb_drone_count"]["state_topic"] == "adsb/summary/drone_count"

    def test_nearest_drone_has_attributes_topic(self) -> None:
        bodies = _bodies_by_unique_id(build_discovery_payloads(self._config_with_drones()))
        body = bodies["adsb_nearest_drone"]
        assert body["state_topic"] == "adsb/summary/nearest_drone"
        assert body["json_attributes_topic"] == "adsb/summary/nearest_drone"

    def test_no_drone_entities_without_remoteid_source(self) -> None:
        payloads = build_discovery_payloads(_make_config())
        assert not any("drone" in t for t in _topics(payloads))
