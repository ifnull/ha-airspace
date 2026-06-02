"""Command-line entry point.

``main(argv)`` parses args, loads + validates config (fail-fast), configures
logging, optionally starts the Prometheus endpoint, builds the app, and runs
it to completion. Returns a process exit code rather than calling ``sys.exit``
directly, so it is unit-testable.

Exit codes:
  0  clean shutdown (SIGTERM / SIGINT / request_stop)
  1  unexpected runtime error
  2  bad config (missing file, invalid schema) — fail-fast at startup

``configure_logging`` runs as early as possible, but config errors can occur
before logging is set up (we don't know the level yet), so the config-error
path prints to stderr directly with a clear message and the field path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

import structlog

from adsb_enrich import __version__
from adsb_enrich.app import build_app
from adsb_enrich.config import Config, ConfigError, load_config
from adsb_enrich.logging import configure_logging, renderer_for
from adsb_enrich.metrics import MetricsRegistry

log = structlog.get_logger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adsb-enrich",
        description="Multi-source ADS-B enrichment: aircraft.json -> MQTT -> Home Assistant.",
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        metavar="PATH",
        help="Path to the YAML config file.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse args, load config, run the service. Returns an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # Logging may not be configured yet, and config errors must always
        # be legible — print straight to stderr and bail.
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(
        level=config.service.log_level,
        renderer=renderer_for(config.service.log_destination),
    )
    log.info(
        "service_starting",
        version=__version__,
        receivers=[r.name for r in config.receivers if r.enabled],
        broker=config.mqtt.broker,
    )

    metrics = MetricsRegistry()
    _maybe_start_metrics(config, metrics)

    try:
        asyncio.run(build_app(config, metrics=metrics).run())
    except KeyboardInterrupt:
        # Defensive: asyncio.run normally translates SIGINT into a clean
        # unwind via the signal handler, but a Ctrl-C landing between
        # run() returning and the loop closing should still exit 0.
        return EXIT_OK
    except Exception:
        log.exception("service_crashed")
        return EXIT_ERROR
    return EXIT_OK


def _maybe_start_metrics(config: Config, metrics: MetricsRegistry) -> None:
    """Start the Prometheus /metrics server if enabled. Off by default;
    localhost-bound unless the operator widens ``prometheus.bind``."""
    if not config.prometheus.enabled:
        return
    metrics.start_server(port=config.prometheus.port, addr=config.prometheus.bind)


__all__ = ["EXIT_CONFIG", "EXIT_ERROR", "EXIT_OK", "build_parser", "main"]
