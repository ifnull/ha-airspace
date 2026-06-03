"""Prometheus metrics surface.

Exposes an optional ``/metrics`` HTTP endpoint (off by default; localhost-
bound when on) plus a ``MetricsRegistry`` that owns every counter / gauge /
histogram the service emits. Receivers, the MQTT client, and the merger
each take a ``MetricsRegistry`` instance and call its attributes directly.

Why a class instead of module-level metrics:

* Tests get a fresh ``CollectorRegistry`` per construction, so global
  state from ``prometheus_client`` never leaks across the test suite.
* The /metrics HTTP server is opt-in; the same registry works whether
  it is exposed or not (Counters keep counting in-memory either way).
* Future work (per-receiver scoping, hot-reload) can subclass without
  reaching into module globals.

Phase 1 only registers metrics for things Phase 1 actually emits:
receiver polls, MQTT publishes, slow-poll skips, broker connection
state. Phase 2 adds journal, alerts, photos, DB refresh; those names
should be added here when those modules land, not pre-allocated as
zero-valued gauges that confuse Grafana dashboards.
"""

from __future__ import annotations

import structlog
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

log = structlog.get_logger(__name__)


class MetricsRegistry:
    """Owns every Phase 1 Prometheus metric and the optional HTTP server.

    Instantiate once at startup; pass to receivers, MQTT client, merger.
    Threadsafe (prometheus_client's metric primitives are).
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        # Custom registry by default so we never collide with the global
        # one — important for tests, and mildly useful in process to
        # avoid double-registration if anything else in the dep tree
        # imports prometheus_client.
        self._registry = registry if registry is not None else CollectorRegistry()
        self._server_started = False

        # --- Per-receiver poll lifecycle -----------------------------------
        self.receiver_polls = Counter(
            "adsb_receiver_polls_total",
            "Receiver poll attempts, by outcome.",
            labelnames=["receiver", "status"],
            registry=self._registry,
        )
        """``status`` is ``"ok"`` | ``"fail"`` | ``"skipped"``. Increment by
        the merger's poll loop after each ``ReceiverSource.fetch()`` call.
        ``"skipped"`` means the prior poll was still in flight when this
        tick fired (slow-poll skip policy)."""

        self.receiver_poll_duration = Histogram(
            "adsb_receiver_poll_duration_seconds",
            "Wall-clock duration of fetch() calls, in seconds.",
            labelnames=["receiver"],
            registry=self._registry,
        )

        self.receiver_aircraft_visible = Gauge(
            "adsb_receiver_aircraft_visible",
            "Aircraft count from the most recent successful poll.",
            labelnames=["receiver"],
            registry=self._registry,
        )

        self.receiver_messages_per_second = Gauge(
            "adsb_receiver_messages_per_second",
            "Receiver-reported decoded-message rate.",
            labelnames=["receiver"],
            registry=self._registry,
        )

        self.receiver_consecutive_failures = Gauge(
            "adsb_receiver_consecutive_failures",
            "Consecutive fetch() failures since last success. Resets to 0 "
            "on the first successful fetch. Receiver is marked unhealthy "
            "in MQTT once this exceeds the failure threshold.",
            labelnames=["receiver"],
            registry=self._registry,
        )

        # --- Aggregate state ------------------------------------------------
        self.aircraft_active = Gauge(
            "adsb_aircraft_active",
            "Currently-tracked aircraft (lifecycle ACTIVE or STALE).",
            labelnames=["band"],
            registry=self._registry,
        )

        # --- MQTT publisher / client ---------------------------------------
        self.mqtt_publishes = Counter(
            "adsb_mqtt_publishes_total",
            "MQTT messages published, by topic class.",
            labelnames=["topic_class"],
            registry=self._registry,
        )
        """``topic_class`` is ``"aircraft"`` | ``"summary"`` | ``"alert"``
        | ``"status"`` | ``"discovery"``. Per-aircraft topics are NOT
        per-hex-labeled; cardinality stays bounded."""

        self.mqtt_drops = Counter(
            "adsb_mqtt_drops_total",
            "Messages dropped by the in-memory publish queue when the "
            "broker was disconnected and the queue hit its overflow cap.",
            registry=self._registry,
        )

        self.mqtt_reconnects = Counter(
            "adsb_mqtt_reconnects_total",
            "Successful broker reconnect events. The first connect at startup does NOT count.",
            registry=self._registry,
        )

        self.mqtt_connected = Gauge(
            "adsb_mqtt_connected",
            "1 if the broker connection is currently alive, 0 otherwise.",
            registry=self._registry,
        )

        # --- Poll-loop scheduling ------------------------------------------
        self.slow_polls = Counter(
            "adsb_slow_polls_total",
            "Polls skipped because the previous poll was still in flight when the next tick fired.",
            labelnames=["receiver"],
            registry=self._registry,
        )

    @property
    def registry(self) -> CollectorRegistry:
        """Underlying ``CollectorRegistry``. Useful for tests that want
        to call ``generate_latest()`` directly."""
        return self._registry

    def start_server(self, port: int = 9090, addr: str = "127.0.0.1") -> None:
        """Start the Prometheus HTTP exposition server in a daemon thread.

        Defaults to ``127.0.0.1:9090`` so a stock install never exposes
        the metrics endpoint to the LAN. Operators who want network-
        accessible metrics set ``addr`` explicitly via config.

        Idempotent: subsequent calls are no-ops, even with different
        port/addr (the first call wins). This keeps the API safe to
        wire into restart paths without bookkeeping.
        """
        if self._server_started:
            log.debug("prometheus_already_started", port=port, addr=addr)
            return
        start_http_server(port, addr=addr, registry=self._registry)
        self._server_started = True
        log.info("prometheus_started", port=port, addr=addr)


__all__ = ["MetricsRegistry"]
