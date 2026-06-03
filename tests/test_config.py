"""Tests for ha_airspace.config.

Covers:
  * Pydantic schema validation: ranges, literals, required fields
  * Strict-mode unknown-key rejection at every nesting depth
  * Cross-field validators: name uniqueness, auth-type consistency
  * load_config() error paths: missing file, malformed YAML, empty,
    non-mapping root, schema failures
  * Conversion to runtime types (Watchpoint)

Tests use temp YAML files for the loader path; in-memory dict construction
for everything else (cheaper, clearer error messages).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ha_airspace.config import (
    AuthConfig,
    Config,
    ConfigError,
    MqttConfig,
    ReceiverConfig,
    ReceiverLocationConfig,
    ServiceConfig,
    WatchpointConfig,
    load_config,
)
from ha_airspace.models import Watchpoint

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


def _minimal_config_dict() -> dict[str, Any]:
    """Smallest config that validates. Single watchpoint, single
    receiver, mqtt broker; everything else uses schema defaults."""
    return {
        "watchpoints": [{"name": "home", "lat": 30.33, "lon": -97.99}],
        "mqtt": {"broker": "broker.local"},
        "receivers": [
            {
                "name": "rx-home",
                "url": "http://localhost:8080/aircraft.json",
                "band": "1090",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Sub-section schemas
# ---------------------------------------------------------------------------


class TestServiceConfig:
    def test_defaults(self) -> None:
        s = ServiceConfig()
        assert s.poll_interval_s == 1.0
        assert s.http_timeout_s == 5.0
        assert s.log_level == "info"
        assert s.log_destination == "stdout"

    def test_log_level_must_be_known(self) -> None:
        with pytest.raises(ValidationError, match="log_level"):
            ServiceConfig(log_level="trace")  # type: ignore[arg-type]

    def test_log_destination_must_be_known(self) -> None:
        with pytest.raises(ValidationError, match="log_destination"):
            ServiceConfig(log_destination="syslog")  # type: ignore[arg-type]

    def test_poll_interval_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="poll_interval_s"):
            ServiceConfig(poll_interval_s=0)
        with pytest.raises(ValidationError, match="poll_interval_s"):
            ServiceConfig(poll_interval_s=-1)

    def test_strict_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match=r"Extra inputs are not permitted"):
            ServiceConfig.model_validate({"poll_interval_s": 1.0, "typo_field": True})


class TestWatchpointConfig:
    def test_minimal(self) -> None:
        wp = WatchpointConfig(name="home", lat=30.33, lon=-97.99)
        assert wp.elevation_m is None

    def test_lat_range(self) -> None:
        with pytest.raises(ValidationError, match="lat"):
            WatchpointConfig(name="x", lat=91.0, lon=0.0)
        with pytest.raises(ValidationError, match="lat"):
            WatchpointConfig(name="x", lat=-91.0, lon=0.0)

    def test_lon_range(self) -> None:
        with pytest.raises(ValidationError, match="lon"):
            WatchpointConfig(name="x", lat=0.0, lon=181.0)
        with pytest.raises(ValidationError, match="lon"):
            WatchpointConfig(name="x", lat=0.0, lon=-181.0)

    def test_name_required_non_empty(self) -> None:
        with pytest.raises(ValidationError, match="name"):
            WatchpointConfig(name="", lat=30.33, lon=-97.99)

    def test_to_runtime_produces_frozen_watchpoint(self) -> None:
        wp = WatchpointConfig(name="home", lat=30.33, lon=-97.99, elevation_m=200.0)
        runtime = wp.to_runtime()
        assert isinstance(runtime, Watchpoint)
        assert runtime.name == "home"
        assert runtime.elevation_m == 200.0


class TestMqttConfig:
    def test_minimal(self) -> None:
        m = MqttConfig(broker="broker.local")
        assert m.port == 1883
        assert m.base_topic == "adsb"
        assert m.discovery_prefix == "homeassistant"
        assert m.discovery_enabled is True
        assert m.tls is False

    def test_port_range(self) -> None:
        with pytest.raises(ValidationError, match="port"):
            MqttConfig(broker="broker.local", port=0)
        with pytest.raises(ValidationError, match="port"):
            MqttConfig(broker="broker.local", port=70000)

    def test_broker_required(self) -> None:
        with pytest.raises(ValidationError, match="broker"):
            MqttConfig.model_validate({})

    def test_throttle_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError, match="publish_aircraft_min_interval_s"):
            MqttConfig(broker="b", publish_aircraft_min_interval_s=-0.1)


class TestAuthConfig:
    def test_default_is_none_type(self) -> None:
        a = AuthConfig()
        assert a.type == "none"

    def test_basic_requires_username_and_password(self) -> None:
        with pytest.raises(ValidationError, match="basic"):
            AuthConfig(type="basic", username="user")
        with pytest.raises(ValidationError, match="basic"):
            AuthConfig(type="basic", password="pw")
        # Both supplied: OK.
        AuthConfig(type="basic", username="user", password="pw")

    def test_header_requires_non_empty_headers(self) -> None:
        with pytest.raises(ValidationError, match="header"):
            AuthConfig(type="header")
        # With headers: OK.
        AuthConfig(type="header", headers={"Authorization": "Bearer x"})

    def test_none_rejects_credentials(self) -> None:
        # If type is none, supplying credentials is a bug to surface
        # — usually means the user forgot to set type=basic.
        with pytest.raises(ValidationError, match="none"):
            AuthConfig(type="none", username="user")
        with pytest.raises(ValidationError, match="none"):
            AuthConfig(type="none", headers={"X": "y"})


class TestReceiverConfig:
    def test_minimal(self) -> None:
        r = ReceiverConfig(name="rx", url="http://x", band="1090")
        assert r.poll_interval_s is None
        assert r.location is None
        assert r.enabled is True
        assert r.auth.type == "none"

    def test_band_must_be_1090_or_978(self) -> None:
        with pytest.raises(ValidationError, match="band"):
            ReceiverConfig(name="rx", url="http://x", band="2400")  # type: ignore[arg-type]

    def test_poll_interval_must_be_positive_when_set(self) -> None:
        with pytest.raises(ValidationError, match="poll_interval_s"):
            ReceiverConfig(name="rx", url="http://x", band="1090", poll_interval_s=0)

    def test_location_validates(self) -> None:
        # Bad location bubbles up the nested validation error.
        with pytest.raises(ValidationError, match="lat"):
            ReceiverConfig.model_validate(
                {
                    "name": "rx",
                    "url": "http://x",
                    "band": "1090",
                    "location": {"lat": 100, "lon": 0},
                }
            )


# ---------------------------------------------------------------------------
# Top-level Config validation
# ---------------------------------------------------------------------------


class TestConfigBasics:
    def test_minimal_config_validates(self) -> None:
        cfg = Config.model_validate(_minimal_config_dict())
        assert len(cfg.watchpoints) == 1
        assert len(cfg.receivers) == 1
        assert cfg.service.poll_interval_s == 1.0  # default
        assert cfg.prometheus.enabled is False  # default

    def test_at_least_one_watchpoint_required(self) -> None:
        d = _minimal_config_dict()
        d["watchpoints"] = []
        with pytest.raises(ValidationError, match="watchpoints"):
            Config.model_validate(d)

    def test_at_least_one_receiver_required(self) -> None:
        d = _minimal_config_dict()
        d["receivers"] = []
        with pytest.raises(ValidationError, match="receivers"):
            Config.model_validate(d)

    def test_mqtt_required(self) -> None:
        d = _minimal_config_dict()
        del d["mqtt"]
        with pytest.raises(ValidationError, match="mqtt"):
            Config.model_validate(d)


class TestConfigStrictMode:
    """Unknown keys at every nesting depth must fail. Typos in YAML are
    the most common config bug class; silent ignore is unacceptable."""

    def test_unknown_top_level_key(self) -> None:
        d = _minimal_config_dict()
        d["typo_section"] = {}
        with pytest.raises(ValidationError, match="typo_section"):
            Config.model_validate(d)

    def test_unknown_service_key(self) -> None:
        d = _minimal_config_dict()
        d["service"] = {"poll_interval_s": 1.0, "tpyo": True}
        with pytest.raises(ValidationError, match="tpyo"):
            Config.model_validate(d)

    def test_unknown_mqtt_key(self) -> None:
        d = _minimal_config_dict()
        d["mqtt"]["unknown_field"] = "x"
        with pytest.raises(ValidationError, match="unknown_field"):
            Config.model_validate(d)

    def test_unknown_receiver_key(self) -> None:
        d = _minimal_config_dict()
        d["receivers"][0]["typo"] = True
        with pytest.raises(ValidationError, match="typo"):
            Config.model_validate(d)


class TestConfigUniqueness:
    def test_duplicate_watchpoint_names_rejected(self) -> None:
        d = _minimal_config_dict()
        d["watchpoints"] = [
            {"name": "home", "lat": 30.33, "lon": -97.99},
            {"name": "home", "lat": 30.40, "lon": -98.00},
        ]
        with pytest.raises(ValidationError, match="unique"):
            Config.model_validate(d)

    def test_duplicate_receiver_names_rejected(self) -> None:
        d = _minimal_config_dict()
        d["receivers"] = [
            {"name": "rx", "url": "http://a", "band": "1090"},
            {"name": "rx", "url": "http://b", "band": "978"},
        ]
        with pytest.raises(ValidationError, match="unique"):
            Config.model_validate(d)

    def test_distinct_names_pass(self) -> None:
        d = _minimal_config_dict()
        d["watchpoints"] = [
            {"name": "home", "lat": 30.33, "lon": -97.99},
            {"name": "office", "lat": 30.40, "lon": -98.00},
        ]
        d["receivers"] = [
            {"name": "rx-1090", "url": "http://a", "band": "1090"},
            {"name": "rx-978", "url": "http://b", "band": "978"},
        ]
        cfg = Config.model_validate(d)
        assert {wp.name for wp in cfg.watchpoints} == {"home", "office"}
        assert {r.name for r in cfg.receivers} == {"rx-1090", "rx-978"}


class TestConfigConversions:
    def test_poll_interval_for_uses_receiver_when_set(self) -> None:
        d = _minimal_config_dict()
        d["service"] = {"poll_interval_s": 1.0}
        d["receivers"][0]["poll_interval_s"] = 5.0
        cfg = Config.model_validate(d)
        assert cfg.poll_interval_for(cfg.receivers[0]) == 5.0

    def test_poll_interval_for_falls_back_to_service(self) -> None:
        d = _minimal_config_dict()
        d["service"] = {"poll_interval_s": 2.5}
        cfg = Config.model_validate(d)
        # receiver.poll_interval_s is None.
        assert cfg.receivers[0].poll_interval_s is None
        assert cfg.poll_interval_for(cfg.receivers[0]) == 2.5

    def test_watchpoints_runtime_returns_frozen_dataclasses(self) -> None:
        cfg = Config.model_validate(_minimal_config_dict())
        runtime = cfg.watchpoints_runtime()
        assert len(runtime) == 1
        assert isinstance(runtime[0], Watchpoint)
        assert runtime[0].name == "home"


# ---------------------------------------------------------------------------
# load_config — file IO + error paths
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def _write(self, tmp_path: Path, contents: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            """
watchpoints:
  - name: home
    lat: 30.33
    lon: -97.99
mqtt:
  broker: broker.local
receivers:
  - name: rx-home
    url: http://localhost:8080/aircraft.json
    band: "1090"
""",
        )
        cfg = load_config(path)
        assert cfg.watchpoints[0].name == "home"
        assert cfg.mqtt.broker == "broker.local"
        assert cfg.receivers[0].band == "1090"

    def test_loads_full_phase1_config(self, tmp_path: Path) -> None:
        # Exercise every Phase 1 section in one file — proxy for the
        # real config users will write, with all defaults overridden.
        path = self._write(
            tmp_path,
            """
service:
  poll_interval_s: 0.5
  http_timeout_s: 3.0
  log_level: debug
  log_destination: stdout

watchpoints:
  - name: home
    lat: 30.33
    lon: -97.99
    elevation_m: 200
  - name: office
    lat: 30.40
    lon: -98.00

mqtt:
  broker: homeassistant.home.arpa
  port: 8883
  username: !!str adsb
  password: !!str secret
  base_topic: adsb
  discovery_prefix: homeassistant
  discovery_enabled: true
  tls: true
  publish_aircraft_min_interval_s: 0.5
  publish_summary_min_interval_s: 2.0

prometheus:
  enabled: true
  bind: 0.0.0.0
  port: 9100

receivers:
  - name: rx-home-1090
    url: http://piaware.home.arpa:8080/skyaware/data/aircraft.json
    band: "1090"
    poll_interval_s: 1.0
    location:
      lat: 30.33
      lon: -97.99
    auth:
      type: none
    enabled: true
""",
        )
        cfg = load_config(path)
        assert cfg.service.log_level == "debug"
        assert cfg.prometheus.enabled is True
        assert cfg.mqtt.tls is True
        assert len(cfg.watchpoints) == 2
        assert cfg.receivers[0].location is not None
        assert isinstance(cfg.receivers[0].location, ReceiverLocationConfig)

    def test_missing_file_raises_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_path_is_directory_raises_config_error(self, tmp_path: Path) -> None:
        # IsADirectoryError is a subclass of OSError. Hits the generic
        # OSError branch that catches permission errors and the like —
        # operators get a clear "could not read" message instead of a
        # naked traceback.
        with pytest.raises(ConfigError, match="could not read"):
            load_config(tmp_path)

    def test_malformed_yaml_raises_config_error(self, tmp_path: Path) -> None:
        # Unbalanced bracket; pyyaml will fail to parse.
        path = self._write(tmp_path, "watchpoints: [[[\n")
        with pytest.raises(ConfigError, match="malformed YAML"):
            load_config(path)

    def test_empty_file_raises_config_error(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "")
        with pytest.raises(ConfigError, match="empty"):
            load_config(path)

    def test_non_mapping_root_raises_config_error(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "- just a list\n- of items\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_config(path)

    def test_schema_validation_failure_includes_file_path(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            """
watchpoints:
  - name: home
    lat: 999  # out of range
    lon: 0
mqtt:
  broker: b
receivers:
  - name: rx
    url: http://x
    band: "1090"
""",
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        # The error message should include both the file path and the
        # field that failed — operators need both to fix the problem.
        msg = str(excinfo.value)
        assert str(path) in msg
        assert "lat" in msg

    def test_unknown_field_at_load_time_fails_with_clear_error(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            """
watchpoints:
  - name: home
    lat: 30
    lon: -97
mqtt:
  broker: b
receivers:
  - name: rx
    url: http://x
    band: "1090"
unknown_section:
  whatever: true
""",
        )
        with pytest.raises(ConfigError, match="unknown_section"):
            load_config(path)
