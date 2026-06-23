"""Tests for the HA add-on config renderer (``addon/render_config.py``).

The renderer is add-on glue outside the ``ha_airspace`` package, so it is loaded
by file path rather than imported. The point of these tests is to keep the
options->YAML translation honest: every rendered config is fed through the real
``load_config`` (a tmp file round-trip), so a drift between the renderer and the
pydantic schema fails here, not in a user's Supervisor log.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

from ha_airspace.config import load_config

_RENDER_PATH = Path(__file__).resolve().parent.parent / "addon" / "render_config.py"


def _load_renderer() -> Any:
    spec = importlib.util.spec_from_file_location("addon_render_config", _RENDER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load_renderer()


def _base_options(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "log_level": "info",
        "mqtt_broker": "core-mosquitto",
        "watchpoints": [{"name": "home", "lat": 30.33, "lon": -75.99}],
        "receivers": [
            {
                "name": "home-1090",
                "url": "http://piaware.local:8080/data/aircraft.json",
                "band": "1090",
            }
        ],
    }
    options.update(overrides)
    return options


def _render_and_load(options: dict[str, Any], tmp_path: Path, **kwargs: Any) -> Any:
    """Build a config dict, write it as YAML, and load it through the real
    validator — proving the rendered shape is actually accepted."""
    cfg = render.build_config(options, **kwargs)
    out = tmp_path / "config.yaml"
    out.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return load_config(out)


class TestBuildConfig:
    def test_minimal_options_produce_valid_config(self, tmp_path: Path) -> None:
        config = _render_and_load(_base_options(), tmp_path)
        assert config.mqtt.broker == "core-mosquitto"
        assert [r.name for r in config.receivers] == ["home-1090"]
        assert config.receivers[0].band == "1090"
        assert [w.name for w in config.watchpoints] == ["home"]
        assert config.service.log_level == "info"

    def test_mqtt_service_fallback_when_broker_blank(self, tmp_path: Path) -> None:
        # Broker omitted -> pulled from the HA Mosquitto service discovery blob.
        options = _base_options()
        del options["mqtt_broker"]
        service = {
            "host": "192.168.1.10",
            "port": 1883,
            "username": "mqtt_user",
            "password": "secret",
        }
        config = _render_and_load(options, tmp_path, mqtt_service=service)
        assert config.mqtt.broker == "192.168.1.10"
        assert config.mqtt.username == "mqtt_user"
        assert config.mqtt.password == "secret"

    def test_explicit_broker_overrides_service(self, tmp_path: Path) -> None:
        service = {"host": "should-not-win", "port": 1883}
        config = _render_and_load(_base_options(), tmp_path, mqtt_service=service)
        assert config.mqtt.broker == "core-mosquitto"

    def test_receiver_basic_auth(self, tmp_path: Path) -> None:
        options = _base_options(
            receivers=[
                {
                    "name": "secured",
                    "url": "https://rx.example/data/aircraft.json",
                    "band": "978",
                    "username": "u",
                    "password": "p",
                }
            ]
        )
        config = _render_and_load(options, tmp_path)
        auth = config.receivers[0].auth
        assert auth.type == "basic"
        assert (auth.username, auth.password) == ("u", "p")

    def test_remoteid_feed(self, tmp_path: Path) -> None:
        options = _base_options(
            remoteid=[{"name": "dump3411", "url": "http://dump3411.local:8754/data/remoteid.json"}]
        )
        config = _render_and_load(options, tmp_path)
        assert [r.name for r in config.remoteid] == ["dump3411"]

    def test_journal_toggle(self, tmp_path: Path) -> None:
        config = _render_and_load(_base_options(enable_journal=True), tmp_path)
        assert config.journal is not None
        assert config.journal.path == "/data/journal.db"

    def test_journal_off_by_default(self, tmp_path: Path) -> None:
        assert _render_and_load(_base_options(), tmp_path).journal is None

    def test_metrics_toggle(self, tmp_path: Path) -> None:
        options = _base_options(enable_metrics=True, metrics_port=9091)
        config = _render_and_load(options, tmp_path)
        assert config.prometheus.enabled is True
        assert config.prometheus.port == 9091

    def test_extra_config_merges_complex_rules(self, tmp_path: Path) -> None:
        # The escape hatch: flags/alerts the structured schema doesn't model.
        extra = """
        enrichment:
          flags:
            emergency:
              squawks: ["7500", "7600", "7700"]
        """
        config = _render_and_load(_base_options(extra_config=extra), tmp_path)
        assert "emergency" in config.enrichment.flags
        assert config.enrichment.flags["emergency"].squawks == ["7500", "7600", "7700"]

    def test_extra_config_can_override_structured(self, tmp_path: Path) -> None:
        # Deep-merge: extra_config wins on conflicting keys.
        config = _render_and_load(
            _base_options(extra_config="mqtt:\n  base_topic: custom"), tmp_path
        )
        assert config.mqtt.broker == "core-mosquitto"  # structured key survives
        assert config.mqtt.base_topic == "custom"  # extra wins

    def test_extra_config_non_mapping_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            render.build_config(_base_options(extra_config="- just\n- a\n- list"))


class TestDeepMerge:
    def test_nested_dicts_merge(self) -> None:
        base = {"mqtt": {"broker": "a", "port": 1883}}
        override = {"mqtt": {"port": 8883, "tls": True}}
        assert render.deep_merge(base, override) == {
            "mqtt": {"broker": "a", "port": 8883, "tls": True}
        }

    def test_lists_replace_not_append(self) -> None:
        base = {"receivers": [{"name": "a"}]}
        override = {"receivers": [{"name": "b"}]}
        assert render.deep_merge(base, override) == {"receivers": [{"name": "b"}]}


# ---------------------------------------------------------------------------
# Batteries-included toggles -> native config (no extra_config needed)
# ---------------------------------------------------------------------------


class TestFeatureToggles:
    def test_all_toggles_produce_valid_config(self, tmp_path: Path) -> None:
        opts = _base_options(
            enable_databases=True,
            enable_emergency=True,
            enable_military=True,
            enable_interesting=True,
            enable_alerts=True,
            alert_distance_nm=25,
            enable_orbit=True,
            orbit_min_turn_deg=270,
            enable_photos=True,
            enable_drone_registry=True,
            enable_spoof_detection=True,
            enable_journal=True,
        )
        config = _render_and_load(opts, tmp_path)  # load_config must accept it
        assert config.orbit.enabled is True
        assert config.photos.enabled is True
        assert config.drone_registry.enabled is True
        assert config.spoof.enabled is True

    def test_databases_expand_with_urls(self, tmp_path: Path) -> None:
        config = _render_and_load(_base_options(enable_databases=True), tmp_path)
        names = {s.name: s.url for s in config.databases.sources}
        assert names.get("mictronics", "").endswith(".csv.gz")
        assert names.get("adsbexchange", "").endswith(".json.gz")

    def test_military_flag_and_nearby_alert(self, tmp_path: Path) -> None:
        opts = _base_options(enable_military=True, enable_databases=True, enable_alerts=True)
        config = _render_and_load(opts, tmp_path)
        assert "military" in config.enrichment.flags
        rules = {r.name: r for r in config.enrichment.alerts.rules}
        assert "military_nearby" in rules
        assert rules["military_nearby"].match.max_distance_nm == 30.0
        assert rules["military_nearby"].match.watchpoint == "home"

    def test_emergency_alerts_anywhere(self, tmp_path: Path) -> None:
        config = _render_and_load(
            _base_options(enable_emergency=True, enable_alerts=True), tmp_path
        )
        rule = {r.name: r for r in config.enrichment.alerts.rules}["emergency_anywhere"]
        assert rule.match.flags == ["emergency"]
        assert rule.match.max_distance_nm is None  # fires regardless of distance

    def test_alert_uses_first_watchpoint_name(self, tmp_path: Path) -> None:
        # Generated distance alerts must resolve even when the watchpoint isn't "home".
        opts = _base_options(
            watchpoints=[{"name": "base", "lat": 30.0, "lon": -75}],
            enable_military=True,
            enable_alerts=True,
        )
        config = _render_and_load(opts, tmp_path)
        assert config.enrichment.alerts.rules[0].match.watchpoint == "base"

    def test_orbit_toggle_adds_section_and_alert(self, tmp_path: Path) -> None:
        opts = _base_options(enable_orbit=True, orbit_min_turn_deg=300, enable_alerts=True)
        config = _render_and_load(opts, tmp_path)
        assert config.orbit.min_turn_deg == 300
        assert "orbiting_nearby" in {r.name for r in config.enrichment.alerts.rules}

    def test_flags_without_alerts(self, tmp_path: Path) -> None:
        # Tagging on, alerting off -> flag present, no alert rules.
        opts = _base_options(enable_military=True, enable_alerts=False)
        config = _render_and_load(opts, tmp_path)
        assert "military" in config.enrichment.flags
        assert config.enrichment.alerts.rules == []

    def test_toggles_off_leave_defaults(self, tmp_path: Path) -> None:
        # Plain base options (no feature toggles) -> unopinionated config.
        config = _render_and_load(_base_options(), tmp_path)
        assert config.enrichment.flags == {}
        assert config.enrichment.alerts.rules == []
        assert config.databases.sources == []
        assert config.orbit.enabled is False
        assert config.photos.enabled is False

    def test_drone_registry_toggle_adds_section(self, tmp_path: Path) -> None:
        config = _render_and_load(_base_options(enable_drone_registry=True), tmp_path)
        assert config.drone_registry.enabled is True

    def test_drone_registry_off_by_default(self, tmp_path: Path) -> None:
        config = _render_and_load(_base_options(), tmp_path)
        assert config.drone_registry.enabled is False

    def test_extra_config_overrides_generated_toggle(self, tmp_path: Path) -> None:
        # extra_config deep-merges last, so it wins over a generated section.
        opts = _base_options(
            enable_orbit=True,
            orbit_min_turn_deg=360,
            extra_config="orbit:\n  min_turn_deg: 90",
        )
        config = _render_and_load(opts, tmp_path)
        assert config.orbit.min_turn_deg == 90  # extra_config overrode the toggle
