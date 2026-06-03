"""MQTT publisher subsystem.

Public surface:

* ``payloads`` — Pydantic models for the JSON published to MQTT topics.
  These are the contract HA and other consumers see; treated as the
  external API surface, separate from ``models.py`` (internal types).
* ``discovery`` — HA MQTT-discovery payload generation. Stateless;
  takes a validated ``Config`` and produces the discovery topic +
  body pairs to publish on connect.
* ``client.MqttClient`` — long-lived ``aiomqtt`` connection wrapper
  with the locked graceful-shutdown protocol, reconnect-with-backoff,
  and the drop-oldest publish queue.
* ``publisher.Publisher`` — topic routing, retention, throttling, and
  the per-aircraft / summary / per-receiver topic taxonomy on top of
  ``MqttClient``.
"""

from __future__ import annotations

from ha_airspace.mqtt.client import MqttClient, OnConnectCallback
from ha_airspace.mqtt.discovery import build_discovery_payloads
from ha_airspace.mqtt.payloads import (
    AircraftPayload,
    ReceiverLocationPayload,
    ReceiverStatsPayload,
)
from ha_airspace.mqtt.publisher import Publisher

__all__ = [
    "AircraftPayload",
    "MqttClient",
    "OnConnectCallback",
    "Publisher",
    "ReceiverLocationPayload",
    "ReceiverStatsPayload",
    "build_discovery_payloads",
]
