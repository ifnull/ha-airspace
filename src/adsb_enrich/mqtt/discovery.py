"""Home Assistant MQTT-discovery payload generation.

HA's MQTT-discovery convention: publish a JSON config payload to
``<discovery_prefix>/<component>/<node_id>/<object_id>/config`` and HA
auto-creates an entity. We use ``node_id="adsb"`` to namespace; entities
are grouped under one device so they appear together in the HA UI.

Phase 1 entities (locked surface — adding per-aircraft entities is an
explicit anti-goal that would explode HA's entity registry):

* ``sensor.adsb_count`` — total active aircraft.
* ``sensor.adsb_nearest`` — closest aircraft (state = distance,
  attributes = full aircraft JSON).
* ``binary_sensor.adsb_<receiver>_status`` — per-receiver online flag.
* ``sensor.adsb_<receiver>_aircraft_count`` — per-receiver count.
* ``sensor.adsb_<receiver>_messages_per_sec`` — per-receiver message
  rate.

Phase 2a will add ``binary_sensor.adsb_alert_<rule>`` for each
configured alert rule. The function shape here is forward-compatible.

Output of ``build_discovery_payloads`` is a list of ``(topic, body)``
tuples. The publisher serializes each ``body`` to JSON and publishes
retained on every successful broker connect (HA discovery is idempotent
over retained topics, and republishing protects against the broker
losing retained state on a restart).
"""

from __future__ import annotations

from typing import Any

from adsb_enrich.config import Config, ReceiverConfig

DISCOVERY_NODE_ID: str = "adsb"
"""Namespace under the discovery prefix; lets multiple ADS-B services
on the same HA instance coexist (just give them different base_topics
and rename the unique_id prefix when that day comes)."""


def build_discovery_payloads(
    config: Config,
    *,
    sw_version: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Build every HA-discovery (topic, body) pair this service publishes.

    Args:
        config: Validated service config.
        sw_version: Optional software version string for the device
            block. Caller normally passes ``adsb_enrich.__version__``.

    Returns:
        List of ``(discovery_topic, payload_dict)`` tuples. The
        publisher serializes each payload to JSON and publishes
        retained.
    """
    if not config.mqtt.discovery_enabled:
        return []

    device_block = _service_device_block(sw_version)
    availability_topic = f"{config.mqtt.base_topic}/status"
    discovery_prefix = config.mqtt.discovery_prefix
    base = config.mqtt.base_topic

    payloads: list[tuple[str, dict[str, Any]]] = []

    # --- Service-wide entities -----------------------------------------
    payloads.append(
        _entity_config(
            discovery_prefix=discovery_prefix,
            component="sensor",
            object_id="count",
            body={
                "name": "ADS-B Aircraft Count",
                "unique_id": "adsb_count",
                "state_topic": f"{base}/summary/count",
                "icon": "mdi:airplane",
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
            },
        )
    )

    payloads.append(
        _entity_config(
            discovery_prefix=discovery_prefix,
            component="sensor",
            object_id="nearest",
            body={
                "name": "ADS-B Nearest Aircraft",
                "unique_id": "adsb_nearest",
                "state_topic": f"{base}/summary/nearest",
                "value_template": ("{{ value_json.distance_to.home if value_json else None }}"),
                "json_attributes_topic": f"{base}/summary/nearest",
                "unit_of_measurement": "nm",
                "icon": "mdi:airplane-marker",
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
            },
        )
    )

    # --- Per-receiver entities -----------------------------------------
    for receiver in config.receivers:
        if not receiver.enabled:
            continue
        payloads.extend(
            _build_receiver_entities(
                receiver=receiver,
                base=base,
                discovery_prefix=discovery_prefix,
                availability_topic=availability_topic,
                device_block=device_block,
            )
        )

    # NO per-aircraft entities. Locked decision (eng review §11):
    # auto-discovering one entity per hex blows up HA's entity registry
    # within a single busy day of traffic. Power users subscribe to
    # `adsb/aircraft/<hex>` directly with their own consumer.

    return payloads


def _build_receiver_entities(
    *,
    receiver: ReceiverConfig,
    base: str,
    discovery_prefix: str,
    availability_topic: str,
    device_block: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Three entities per receiver: status, count, message rate."""
    rx_topic_base = f"{base}/receiver/{receiver.name}"
    name_prefix = f"ADS-B Receiver {receiver.name}"

    return [
        _entity_config(
            discovery_prefix=discovery_prefix,
            component="binary_sensor",
            object_id=f"receiver_{receiver.name}_status",
            body={
                "name": f"{name_prefix} Status",
                "unique_id": f"adsb_receiver_{receiver.name}_status",
                "state_topic": f"{rx_topic_base}/status",
                "payload_on": "online",
                "payload_off": "offline",
                "device_class": "connectivity",
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
            },
        ),
        _entity_config(
            discovery_prefix=discovery_prefix,
            component="sensor",
            object_id=f"receiver_{receiver.name}_aircraft_count",
            body={
                "name": f"{name_prefix} Aircraft Count",
                "unique_id": f"adsb_receiver_{receiver.name}_aircraft_count",
                "state_topic": f"{rx_topic_base}/stats",
                "value_template": "{{ value_json.aircraft_count }}",
                "icon": "mdi:airplane",
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
            },
        ),
        _entity_config(
            discovery_prefix=discovery_prefix,
            component="sensor",
            object_id=f"receiver_{receiver.name}_messages_per_sec",
            body={
                "name": f"{name_prefix} Message Rate",
                "unique_id": f"adsb_receiver_{receiver.name}_messages_per_sec",
                "state_topic": f"{rx_topic_base}/stats",
                "value_template": "{{ value_json.messages_per_sec }}",
                "unit_of_measurement": "msg/s",
                "icon": "mdi:radio-tower",
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
            },
        ),
    ]


def _entity_config(
    *,
    discovery_prefix: str,
    component: str,
    object_id: str,
    body: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Build the (topic, body) pair for a single discovery message.

    HA discovery topic format:
    ``<discovery_prefix>/<component>/<node_id>/<object_id>/config``
    """
    topic = f"{discovery_prefix}/{component}/{DISCOVERY_NODE_ID}/{object_id}/config"
    return topic, body


def _service_device_block(sw_version: str | None) -> dict[str, Any]:
    """Device block shared by every service-wide entity. HA groups
    entities with the same identifier under one device card."""
    block: dict[str, Any] = {
        "identifiers": ["adsb_enrich"],
        "name": "ADS-B Enrich",
        "manufacturer": "ifnull/ha-squitter",
        "model": "adsb-enrich",
    }
    if sw_version is not None:
        block["sw_version"] = sw_version
    return block


__all__ = ["build_discovery_payloads"]
