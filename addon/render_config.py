#!/usr/bin/env python3
"""Translate Home Assistant add-on options into a ha-airspace YAML config.

The Supervisor writes the user's add-on options to ``/data/options.json``. This
renders them into the service's native ``config.yaml`` and writes it where the
run shim points the service at. Kept deliberately outside the ``ha_airspace``
package — it is add-on glue, not part of the service — but it produces a config
the real ``load_config`` validates, so ``tests/test_addon_render.py`` exercises
``build_config`` against the actual pydantic schema.

Translation posture: structured options cover the essentials (receivers,
watchpoints, MQTT) plus batteries-included *toggles* that expand into the common
enrichment (databases, curated flags + alerts, orbit, photos) — so normal use
needs no hand-written YAML. The open-ended rule grammar isn't re-modeled in HA's
schema language; ``extra_config`` is the raw-YAML override for *custom* rules or
to tweak a generated value, deep-merged last over everything above. MQTT
connection details fall back to the HA Mosquitto service when left blank.

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

# Batteries-included defaults the flat toggles expand to. These live here (the
# add-on layer), not the core config, so Docker/pip users stay unopinionated
# while the add-on works out of the box without hand-written YAML. extra_config
# still deep-merges last, so any of this can be overridden.
_MICTRONICS_URL = "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
_ADSBEXCHANGE_URL = "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz"

# toggle option -> the flag rule it produces. The DB-backed ones (sources:)
# only match when enable_databases is on; emergency is squawk-based and needs no DB.
_CURATED_FLAGS: dict[str, dict[str, Any]] = {
    "emergency": {"squawks": ["7500", "7600", "7700"]},
    "military": {"sources": ["adsbexchange:mil"]},
    "interesting": {"sources": ["adsbexchange:pia", "adsbexchange:ladd"]},
}
_DEFAULT_ALERT_DISTANCE_NM = 30.0
_DEFAULT_ORBIT_MIN_TURN_DEG = 360.0


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


def _enabled_flag_names(options: dict[str, Any]) -> list[str]:
    """Curated flags the toggles turn on, in a stable order."""
    return [name for name in _CURATED_FLAGS if options.get(f"enable_{name}")]


def _alert_rules(options: dict[str, Any], flag_names: list[str], default_wp: str) -> list[dict]:
    """One alert rule per enabled flag (+ orbiting). Emergency alerts anywhere;
    the rest fire within ``alert_distance_nm`` of the primary watchpoint."""
    distance = options.get("alert_distance_nm") or _DEFAULT_ALERT_DISTANCE_NM
    rules: list[dict[str, Any]] = []
    for name in flag_names:
        if name == "emergency":
            rules.append({"name": "emergency_anywhere", "match": {"flags": ["emergency"]}})
        else:
            rules.append(
                {
                    "name": f"{name}_nearby",
                    "match": {
                        "flags": [name],
                        "max_distance_nm": distance,
                        "watchpoint": default_wp,
                    },
                }
            )
    if options.get("enable_orbit"):
        rules.append(
            {
                "name": "orbiting_nearby",
                "match": {
                    "flags": ["orbiting"],
                    "max_distance_nm": distance,
                    "watchpoint": default_wp,
                },
            }
        )
    return rules


def _feature_sections(options: dict[str, Any], default_wp: str) -> dict[str, Any]:
    """Expand the batteries-included toggles into native config sections
    (databases, enrichment flags/alerts, orbit, photos, drone_registry).
    extra_config can still override any of these afterwards."""
    sections: dict[str, Any] = {}

    if options.get("enable_databases"):
        sections["databases"] = {
            "sources": [
                {"name": "mictronics", "url": _MICTRONICS_URL, "enabled": True},
                {"name": "adsbexchange", "url": _ADSBEXCHANGE_URL, "enabled": True},
            ]
        }

    flag_names = _enabled_flag_names(options)
    enrichment: dict[str, Any] = {}
    if flag_names:
        enrichment["flags"] = {name: dict(_CURATED_FLAGS[name]) for name in flag_names}
    if options.get("enable_alerts") and (rules := _alert_rules(options, flag_names, default_wp)):
        enrichment["alerts"] = {"rules": rules}
    if enrichment:
        sections["enrichment"] = enrichment

    if options.get("enable_orbit"):
        sections["orbit"] = {
            "enabled": True,
            "min_turn_deg": options.get("orbit_min_turn_deg") or _DEFAULT_ORBIT_MIN_TURN_DEG,
        }
    if options.get("enable_photos"):
        sections["photos"] = {"enabled": True}
    if options.get("enable_drone_registry"):
        # FAA UAS make/model lookup by broadcast serial. Only meaningful with a
        # remoteid feed configured; harmless (never queried) without one.
        sections["drone_registry"] = {"enabled": True}
    if options.get("enable_spoof_detection"):
        # Tier-1 Remote ID spoof flag (malformed serial / shared self_id). Only
        # acts on remoteid tracks; harmless without a feed.
        sections["spoof"] = {"enabled": True}

    return sections


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

    # Batteries-included feature toggles -> native sections. Keyed on the first
    # watchpoint so the generated alerts resolve even if it isn't named "home".
    default_wp = watchpoints[0]["name"] if watchpoints else "home"
    cfg.update(_feature_sections(options, default_wp))

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
