#!/usr/bin/env python3
"""Translate Home Assistant add-on options into a ha-airspace YAML config.

The Supervisor writes the user's add-on options to ``/data/options.json``. This
renders them into the service's native ``config.yaml`` and writes it where the
run shim points the service at. Kept deliberately outside the ``ha_airspace``
package — it is add-on glue, not part of the service — but it produces a config
the real ``load_config`` validates, so ``tests/test_addon_render.py`` exercises
``build_config`` against the actual pydantic schema.

Translation posture: structured options cover the essentials every user must set
(receivers, watchpoints, MQTT). The complex rule grammar (flags, alerts,
databases) is not re-modeled in HA's schema language — power users supply it via
the ``extra_config`` raw-YAML escape hatch, which is deep-merged over the
structured base. MQTT connection details fall back to the HA Mosquitto service
when left blank.

Usage: ``render_config.py <options.json> <out.yaml>``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any

import yaml

SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"
JOURNAL_PATH = "/data/journal.db"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base``. Nested mappings merge;
    everything else (lists, scalars) is replaced wholesale by ``override``."""
    result = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _mqtt_block(options: dict[str, Any], svc: dict[str, Any]) -> dict[str, Any]:
    """MQTT settings, falling back to the HA Mosquitto service for any
    connection field the user left blank."""
    mqtt: dict[str, Any] = {}
    if broker := (options.get("mqtt_broker") or svc.get("host")):
        mqtt["broker"] = broker
    if port := (options.get("mqtt_port") or svc.get("port")):
        mqtt["port"] = int(port)
    if username := (options.get("mqtt_username") or svc.get("username")):
        mqtt["username"] = username
    if password := (options.get("mqtt_password") or svc.get("password")):
        mqtt["password"] = password
    if base_topic := options.get("mqtt_base_topic"):
        mqtt["base_topic"] = base_topic
    if svc.get("ssl"):
        mqtt["tls"] = True
    return mqtt


def _watchpoints(options: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for wp in options.get("watchpoints", []):
        entry: dict[str, Any] = {"name": wp["name"], "lat": wp["lat"], "lon": wp["lon"]}
        if wp.get("elevation_m") is not None:
            entry["elevation_m"] = wp["elevation_m"]
        out.append(entry)
    return out


def _receivers(options: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in options.get("receivers", []):
        entry: dict[str, Any] = {"name": r["name"], "url": r["url"], "band": str(r["band"])}
        if r.get("username") and r.get("password"):
            entry["auth"] = {"type": "basic", "username": r["username"], "password": r["password"]}
        out.append(entry)
    return out


def build_config(
    options: dict[str, Any], mqtt_service: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Project add-on options (+ optional MQTT service info) into a config dict
    that ``ha_airspace.config.load_config`` accepts. Pure and IO-free so it is
    unit-testable; the Supervisor call lives in ``fetch_mqtt_service``."""
    svc = mqtt_service or {}
    cfg: dict[str, Any] = {}

    if log_level := options.get("log_level"):
        cfg["service"] = {"log_level": log_level}
    if watchpoints := _watchpoints(options):
        cfg["watchpoints"] = watchpoints
    if mqtt := _mqtt_block(options, svc):
        cfg["mqtt"] = mqtt
    if receivers := _receivers(options):
        cfg["receivers"] = receivers
    if remoteid := [{"name": r["name"], "url": r["url"]} for r in options.get("remoteid", [])]:
        cfg["remoteid"] = remoteid
    # Journal defaults to /data (persistent, SD-card-friendly coalesced writes).
    if options.get("enable_journal"):
        cfg["journal"] = {"path": JOURNAL_PATH}
    if options.get("enable_metrics"):
        # bind 0.0.0.0 inside the container: the add-on exposes the port via HA,
        # not directly on the host.
        cfg["prometheus"] = {
            "enabled": True,
            "bind": "0.0.0.0",
            "port": int(options.get("metrics_port", 9090)),
        }
    if extra := options.get("extra_config"):
        parsed = yaml.safe_load(extra) or {}
        if not isinstance(parsed, dict):
            raise ValueError("extra_config must be a YAML mapping (key: value)")
        cfg = deep_merge(cfg, parsed)
    return cfg


def fetch_mqtt_service() -> dict[str, Any] | None:
    """Best-effort: read MQTT connection info from the HA Mosquitto service via
    the Supervisor API. Returns ``None`` if unavailable (no token, no service,
    or any error) — the user can always set MQTT options explicitly instead."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    req = urllib.request.Request(SUPERVISOR_MQTT_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 — best-effort discovery, never fatal
        print(f"[render] MQTT service discovery skipped: {exc}", file=sys.stderr)
        return None
    return body.get("data") if isinstance(body, dict) else None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: render_config.py <options.json> <out.yaml>", file=sys.stderr)
        return 2
    options_path, out_path = argv[1], argv[2]
    with open(options_path) as fh:
        options = json.load(fh)

    mqtt_service = None
    if not options.get("mqtt_broker"):
        mqtt_service = fetch_mqtt_service()

    config = build_config(options, mqtt_service)
    with open(out_path, "w") as fh:
        yaml.safe_dump(config, fh, sort_keys=False, default_flow_style=False)
    print(f"[render] wrote {out_path} ({len(config)} top-level sections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
