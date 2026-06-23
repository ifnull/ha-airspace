"""Home Assistant MQTT-discovery payload generation.

HA's MQTT-discovery convention: publish a JSON config payload to
``<discovery_prefix>/<component>/<node_id>/<object_id>/config`` and HA
auto-creates an entity. We use ``node_id="airspace"`` to namespace; entities
are grouped under one device so they appear together in the HA UI.

Phase 1 entities (locked surface — adding per-aircraft entities is an
explicit anti-goal that would explode HA's entity registry):

* ``sensor.airspace_count`` — total active aircraft.
* ``sensor.airspace_nearest`` — closest aircraft (state = distance,
  attributes = full aircraft JSON).
* ``binary_sensor.airspace_<receiver>_status`` — per-receiver online flag.
* ``sensor.airspace_<receiver>_aircraft_count`` — per-receiver count.
* ``sensor.airspace_<receiver>_messages_per_sec`` — per-receiver message
  rate.

Phase 2a will add ``binary_sensor.airspace_alert_<rule>`` for each
configured alert rule. The function shape here is forward-compatible.

Output of ``build_discovery_payloads`` is a list of ``(topic, body)``
tuples. The publisher serializes each ``body`` to JSON and publishes
retained on every successful broker connect (HA discovery is idempotent
over retained topics, and republishing protects against the broker
losing retained state on a restart).
"""

from __future__ import annotations

from typing import Any

from ha_airspace.config import Config
from ha_airspace.orbit import ORBIT_FLAG
from ha_airspace.spoof import SPOOF_FLAG

DISCOVERY_NODE_ID: str = "airspace"
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
            block. Caller normally passes ``ha_airspace.__version__``.

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
                "name": "Aircraft Count",
                "unique_id": "airspace_count",
                "state_topic": f"{base}/summary/count",
                "state_class": "measurement",
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
                "name": "Nearest Aircraft",
                "unique_id": "airspace_nearest",
                "state_topic": f"{base}/summary/nearest",
                "value_template": ("{{ value_json.distance_to.home if value_json else None }}"),
                "json_attributes_topic": f"{base}/summary/nearest",
                "unit_of_measurement": "nm",
                "state_class": "measurement",
                "suggested_display_precision": 1,
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
                name=receiver.name,
                base=base,
                discovery_prefix=discovery_prefix,
                availability_topic=availability_topic,
                device_block=device_block,
            )
        )

    # --- Per-Remote-ID-feed entities -----------------------------------
    # Remote ID feeds publish the same status/stats under receiver/<name>; give
    # them matching entities so a drone feed's health shows up like a receiver.
    for feed in config.remoteid:
        if not feed.enabled:
            continue
        payloads.extend(
            _build_receiver_entities(
                name=feed.name,
                base=base,
                discovery_prefix=discovery_prefix,
                availability_topic=availability_topic,
                device_block=device_block,
                name_prefix=f"Remote ID {feed.name}",
                count_name="Drone Count",
                count_icon="mdi:quadcopter",
            )
        )

    # --- Per-alert-rule binary sensors ---------------------------------
    for rule in config.enrichment.alerts.rules:
        payloads.append(
            _entity_config(
                discovery_prefix=discovery_prefix,
                component="binary_sensor",
                object_id=f"alert_{rule.name}",
                body={
                    "name": f"Alert {rule.name}",
                    "unique_id": f"airspace_alert_{rule.name}",
                    "state_topic": f"{base}/alert/{rule.name}/active",
                    "payload_on": "on",
                    "payload_off": "off",
                    # Triggering track's summary (flight, distance, entity_picture
                    # from the Planespotters photo) -> entity attributes, for a
                    # picture/glance alert card.
                    "json_attributes_topic": f"{base}/alert/{rule.name}/info",
                    "device_class": "safety",
                    "availability_topic": availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": device_block,
                },
            )
        )

    # --- Per-flag feed sensors -----------------------------------------
    # One sensor per configured flag (+ derived 'orbiting' when orbit is on):
    # state = count carrying the flag, attributes = a capped, distance-sorted
    # list of those aircraft (alt / distance / type / squawk / flags). Lets a
    # card list "the military / interesting / ladd aircraft right now" without
    # per-aircraft entities. Kept in sync with the tracker's feed_flags set.
    feed_flags = list(config.enrichment.flags)
    if config.orbit.enabled:
        feed_flags.append(ORBIT_FLAG)
    if config.spoof.enabled:
        feed_flags.append(SPOOF_FLAG)
    for flag in feed_flags:
        payloads.append(
            _entity_config(
                discovery_prefix=discovery_prefix,
                component="sensor",
                object_id=f"flag_{flag}",
                body={
                    "name": f"Flag {flag}",
                    "unique_id": f"airspace_flag_{flag}",
                    "state_topic": f"{base}/summary/by_flag/{flag}",
                    "value_template": "{{ value_json.count if value_json else 0 }}",
                    "json_attributes_topic": f"{base}/summary/by_flag/{flag}",
                    "state_class": "measurement",
                    "icon": "mdi:airplane-alert",
                    "availability_topic": availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": device_block,
                },
            )
        )

    # --- Drone (Remote ID) entities ------------------------------------
    # Only when a Remote ID source is configured — distinct from aircraft so
    # HA users can treat drones and their operators independently.
    if any(rc.enabled for rc in config.remoteid):
        payloads.extend(
            _build_drone_entities(
                base=base,
                discovery_prefix=discovery_prefix,
                availability_topic=availability_topic,
                device_block=device_block,
            )
        )

    # NO per-aircraft / per-drone-track entities. Locked decision (eng review
    # §11): auto-discovering one entity per hex/track blows up HA's entity
    # registry within a single busy day. Power users subscribe to
    # `airspace/aircraft/<id>` / `airspace/drone/<id>` directly with their own consumer.

    return payloads


def _build_drone_entities(
    *,
    base: str,
    discovery_prefix: str,
    availability_topic: str,
    device_block: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Drone count + nearest-drone (with operator location in attributes).

    Distinct from the aircraft entities: a Remote ID drone is not an aircraft,
    and its operator location is a security-relevant field HA users will want
    to surface separately."""
    return [
        _entity_config(
            discovery_prefix=discovery_prefix,
            component="sensor",
            object_id="drone_count",
            body={
                "name": "Drone Count",
                "unique_id": "airspace_drone_count",
                "state_topic": f"{base}/summary/drone_count",
                "state_class": "measurement",
                "icon": "mdi:quadcopter",
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
            },
        ),
        _entity_config(
            discovery_prefix=discovery_prefix,
            component="sensor",
            object_id="nearest_drone",
            body={
                "name": "Nearest Drone",
                "unique_id": "airspace_nearest_drone",
                "state_topic": f"{base}/summary/nearest_drone",
                "value_template": "{{ value_json.distance_to.home if value_json else None }}",
                "json_attributes_topic": f"{base}/summary/nearest_drone",
                "unit_of_measurement": "nm",
                "state_class": "measurement",
                "suggested_display_precision": 1,
                "icon": "mdi:quadcopter",
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
            },
        ),
    ]


def _build_receiver_entities(
    *,
    name: str,
    base: str,
    discovery_prefix: str,
    availability_topic: str,
    device_block: dict[str, Any],
    name_prefix: str | None = None,
    count_name: str = "Aircraft Count",
    count_icon: str = "mdi:airplane",
) -> list[tuple[str, dict[str, Any]]]:
    """Three entities (status, count, message rate) for an ADS-B receiver or a
    Remote ID feed — both publish under ``receiver/<name>/{status,stats}``, so a
    feed just overrides the display labels (``name_prefix`` / ``count_name`` /
    ``count_icon``); the unique_ids/topics keep the ``receiver_<name>`` scheme."""
    rx_topic_base = f"{base}/receiver/{name}"
    prefix = name_prefix or f"Receiver {name}"

    return [
        _entity_config(
            discovery_prefix=discovery_prefix,
            component="binary_sensor",
            object_id=f"receiver_{name}_status",
            body={
                "name": f"{prefix} Status",
                "unique_id": f"airspace_receiver_{name}_status",
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
            object_id=f"receiver_{name}_aircraft_count",
            body={
                "name": f"{prefix} {count_name}",
                "unique_id": f"airspace_receiver_{name}_aircraft_count",
                "state_topic": f"{rx_topic_base}/stats",
                "value_template": "{{ value_json.aircraft_count }}",
                "state_class": "measurement",
                "icon": count_icon,
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
            },
        ),
        _entity_config(
            discovery_prefix=discovery_prefix,
            component="sensor",
            object_id=f"receiver_{name}_messages_per_sec",
            body={
                "name": f"{prefix} Message Rate",
                "unique_id": f"airspace_receiver_{name}_messages_per_sec",
                "state_topic": f"{rx_topic_base}/stats",
                "value_template": "{{ value_json.messages_per_sec }}",
                "unit_of_measurement": "msg/s",
                "state_class": "measurement",
                "suggested_display_precision": 1,
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

    Note: HA derives the *entity_id* from ``device.name`` + entity ``name``
    (slugified) — e.g. device "Airspace" + "Aircraft Count" ->
    ``sensor.airspace_aircraft_count``. We don't set a payload ``object_id`` to
    override that: it isn't honored consistently across HA versions, so relying
    on it would make entity_ids differ between installs. The ``unique_id`` keeps
    entities stable across renames regardless.
    """
    topic = f"{discovery_prefix}/{component}/{DISCOVERY_NODE_ID}/{object_id}/config"
    return topic, body


def _service_device_block(sw_version: str | None) -> dict[str, Any]:
    """Device block shared by every service-wide entity. HA groups
    entities with the same identifier under one device card."""
    block: dict[str, Any] = {
        "identifiers": ["ha_airspace"],
        "name": "Airspace",
        "manufacturer": "ifnull/ha-airspace",
        "model": "ha-airspace",
    }
    if sw_version is not None:
        block["sw_version"] = sw_version
    return block


__all__ = ["build_discovery_payloads"]
