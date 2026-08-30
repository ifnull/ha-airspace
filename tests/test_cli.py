"""Tests for ha_airspace.cli + ha_airspace.logging.

main() is exercised without touching the network by monkeypatching
build_app to return a fake app whose run() returns immediately. Config
loading, exit codes, logging configuration, and the Prometheus gate are
all covered.
"""

from __future__ import annotations

import faulthandler
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import structlog

import ha_airspace.__main__ as main_module
from ha_airspace import cli
from ha_airspace.logging import configure_logging, renderer_for

_VALID_CONFIG = """\
service:
  poll_interval_s: 1.0
watchpoints:
  - name: home
    lat: 30.33
    lon: -75.99
mqtt:
  broker: broker.local
receivers:
  - name: rx-home
    url: http://piaware/aircraft.json
    band: "1090"
"""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeApp:
    """Stand-in App whose run() records that it ran and returns at once."""

    ran: bool = False

    async def run(self) -> None:
        FakeApp.ran = True


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(_VALID_CONFIG, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_fakeapp() -> None:
    FakeApp.ran = False


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestParser:
    def test_config_required(self) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_config_parsed(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["--config", "/tmp/x.yaml"])
        assert args.config == "/tmp/x.yaml"

    def test_short_flag(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["-c", "/tmp/x.yaml"])
        assert args.config == "/tmp/x.yaml"

    def test_version_exits_zero(self) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--version"])
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# main() exit codes
# ---------------------------------------------------------------------------


class TestMain:
    def test_clean_run_returns_ok(
        self, valid_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "build_app", lambda config, *, metrics=None: FakeApp())
        rc = cli.main(["--config", str(valid_config)])
        assert rc == cli.EXIT_OK
        assert FakeApp.ran is True

    def test_missing_config_returns_config_exit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["--config", str(tmp_path / "nope.yaml")])
        assert rc == cli.EXIT_CONFIG
        err = capsys.readouterr().err
        assert "config error" in err

    def test_invalid_config_returns_config_exit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("watchpoints: []\nmqtt: {}\n", encoding="utf-8")  # missing required
        rc = cli.main(["--config", str(bad)])
        assert rc == cli.EXIT_CONFIG
        assert "config error" in capsys.readouterr().err

    def test_runtime_crash_returns_error_exit(
        self, valid_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class CrashingApp:
            async def run(self) -> None:
                raise RuntimeError("boom")

        monkeypatch.setattr(cli, "build_app", lambda config, *, metrics=None: CrashingApp())
        rc = cli.main(["--config", str(valid_config)])
        assert rc == cli.EXIT_ERROR

    def test_keyboard_interrupt_returns_ok(
        self, valid_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class InterruptedApp:
            async def run(self) -> None:
                raise KeyboardInterrupt

        monkeypatch.setattr(cli, "build_app", lambda config, *, metrics=None: InterruptedApp())
        rc = cli.main(["--config", str(valid_config)])
        assert rc == cli.EXIT_OK


# ---------------------------------------------------------------------------
# Prometheus gate
# ---------------------------------------------------------------------------


class TestPrometheusGate:
    def test_metrics_server_started_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            _VALID_CONFIG + "prometheus:\n  enabled: true\n  port: 9123\n",
            encoding="utf-8",
        )
        started: dict[str, Any] = {}

        def fake_start(self: Any, port: int = 9090, addr: str = "127.0.0.1") -> None:
            started["port"] = port
            started["addr"] = addr

        monkeypatch.setattr("ha_airspace.metrics.MetricsRegistry.start_server", fake_start)
        monkeypatch.setattr(cli, "build_app", lambda config, *, metrics=None: FakeApp())
        cli.main(["--config", str(cfg)])
        assert started == {"port": 9123, "addr": "127.0.0.1"}

    def test_metrics_server_not_started_by_default(
        self, valid_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        def fake_start(self: Any, port: int = 9090, addr: str = "127.0.0.1") -> None:
            nonlocal called
            called = True

        monkeypatch.setattr("ha_airspace.metrics.MetricsRegistry.start_server", fake_start)
        monkeypatch.setattr(cli, "build_app", lambda config, *, metrics=None: FakeApp())
        cli.main(["--config", str(valid_config)])
        assert called is False


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


class TestLogging:
    def test_renderer_for_mapping(self) -> None:
        assert renderer_for("stdout") == "json"
        assert renderer_for("file") == "console"

    def test_json_renderer_emits_parseable_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="info", renderer="json")
        structlog.get_logger().info("test_event", answer=42)
        out = capsys.readouterr().out.strip().splitlines()[-1]
        parsed = json.loads(out)
        assert parsed["event"] == "test_event"
        assert parsed["answer"] == 42
        assert parsed["level"] == "info"

    def test_level_filters_below_threshold(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="warning", renderer="json")
        log = structlog.get_logger()
        log.info("suppressed_event")
        log.warning("shown_event")
        out = capsys.readouterr().out
        assert "suppressed_event" not in out
        assert "shown_event" in out

    def test_unknown_level_defaults_to_info(self) -> None:
        # Must not raise; startup logging is defensive.
        configure_logging(level="nonsense", renderer="json")
        assert logging.getLogger().level == logging.INFO

    def test_httpx_capped_at_warning_when_not_debug(self) -> None:
        # httpx's per-request INFO line floods the console at ~1 Hz polling.
        configure_logging(level="info", renderer="json")
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING

    def test_httpx_not_capped_at_debug(self) -> None:
        # A debugging operator who asked for debug should still see requests.
        configure_logging(level="debug", renderer="json")
        assert logging.getLogger("httpx").level == logging.DEBUG


# ---------------------------------------------------------------------------
# module shim
# ---------------------------------------------------------------------------


def test_main_module_exposes_entry() -> None:
    # `python -m ha_airspace` imports this; ensure the entry point is wired.
    assert main_module.main is cli.main


class TestFaultHandler:
    """The diagnostic that separates "kernel SIGKILL'd us" from "we faulted"."""

    def test_enable_turns_it_on(self) -> None:
        faulthandler.disable()
        try:
            cli._enable_faulthandler()
            assert faulthandler.is_enabled()
        finally:
            faulthandler.enable()

    def test_failure_to_enable_does_not_break_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A sandbox with an unusable stderr must not take the service down.
        def boom() -> None:
            raise RuntimeError("no dupable stderr here")

        monkeypatch.setattr("ha_airspace.cli.faulthandler.enable", boom)
        cli._enable_faulthandler()  # must not raise
