"""MQTT publisher subsystem.

Public surface (built up across two commits):

* ``payloads`` — Pydantic models for the JSON published to MQTT topics.
  These are the contract HA and other consumers see; treated as the
  external API surface, separate from ``models.py`` (internal types).
* ``discovery`` — HA MQTT-discovery payload generation. Stateless;
  takes a validated ``Config`` and produces the discovery topic +
  body pairs to publish on connect.
* ``client`` — long-lived ``aiomqtt`` connection wrapper with the
  locked graceful-shutdown protocol. (Next commit.)
* ``publisher`` — topic routing, retention, throttling, drop-oldest
  queue management. (Next commit.)
"""

from __future__ import annotations

from adsb_enrich.mqtt.discovery import build_discovery_payloads
from adsb_enrich.mqtt.payloads import (
    AircraftPayload,
    ReceiverLocationPayload,
    ReceiverStatsPayload,
)

__all__ = [
    "AircraftPayload",
    "ReceiverLocationPayload",
    "ReceiverStatsPayload",
    "build_discovery_payloads",
]
