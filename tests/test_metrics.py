"""Tests for ha_airspace.metrics.

Each test gets its own MetricsRegistry with an explicit CollectorRegistry,
so prometheus_client global state stays out of the suite. The HTTP server
is mocked out — actually binding ports is integration-test territory.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from ha_airspace.metrics import MetricsRegistry


@pytest.fixture
def metrics() -> MetricsRegistry:
    """Fresh registry per test — no shared state with prometheus_client.REGISTRY."""
    return MetricsRegistry(registry=CollectorRegistry())


# ---------------------------------------------------------------------------
# Construction + metric surface
# ---------------------------------------------------------------------------


class TestRegistryConstruction:
    def test_creates_default_registry_when_none_provided(self) -> None:
        m = MetricsRegistry()
        # The default-constructed registry is a CollectorRegistry, not the
        # global prometheus_client.REGISTRY (which would risk
        # cross-construction collisions).
        assert isinstance(m.registry, CollectorRegistry)

    def test_uses_provided_registry(self) -> None:
        injected = CollectorRegistry()
        m = MetricsRegistry(registry=injected)
        assert m.registry is injected

    def test_separate_instances_isolate_state(self) -> None:
        # Two registries must not see each other's increments — important
        # for tests but also for any future hot-reload scenario.
        a = MetricsRegistry(registry=CollectorRegistry())
        b = MetricsRegistry(registry=CollectorRegistry())

        a.mqtt_drops.inc()
        a.mqtt_drops.inc()
        b.mqtt_drops.inc()

        assert _value_of(a, "adsb_mqtt_drops_total") == 2.0
        assert _value_of(b, "adsb_mqtt_drops_total") == 1.0


class TestMetricSurface:
    """Every metric Phase 1 code paths increment must be present and
    typed correctly. If a metric name or label set drifts, downstream
    Grafana dashboards break silently — pin them here.
    """

    def test_all_phase1_metric_names_present(self, metrics: MetricsRegistry) -> None:
        exposition = generate_latest(metrics.registry).decode("utf-8")
        expected = [
            "adsb_receiver_polls_total",
            "adsb_receiver_poll_duration_seconds",
            "adsb_receiver_aircraft_visible",
            "adsb_receiver_messages_per_second",
            "adsb_receiver_consecutive_failures",
            "adsb_aircraft_active",
            "adsb_mqtt_publishes_total",
            "adsb_mqtt_drops_total",
            "adsb_mqtt_reconnects_total",
            "adsb_mqtt_connected",
            "adsb_slow_polls_total",
        ]
        for name in expected:
            assert name in exposition, f"missing metric in exposition: {name}"

    def test_receiver_polls_label_set(self, metrics: MetricsRegistry) -> None:
        # Three labels (receiver, status) — locked surface used by the
        # poll loop. Adding a label is a breaking change.
        metrics.receiver_polls.labels(receiver="rx-home", status="ok").inc()
        metrics.receiver_polls.labels(receiver="rx-home", status="fail").inc(2)
        metrics.receiver_polls.labels(receiver="rx-home", status="skipped").inc()

        text = generate_latest(metrics.registry).decode("utf-8")
        assert 'adsb_receiver_polls_total{receiver="rx-home",status="ok"} 1.0' in text
        assert 'adsb_receiver_polls_total{receiver="rx-home",status="fail"} 2.0' in text
        assert 'adsb_receiver_polls_total{receiver="rx-home",status="skipped"} 1.0' in text

    def test_mqtt_publishes_topic_class_label(self, metrics: MetricsRegistry) -> None:
        for klass in ("aircraft", "summary", "alert", "status"):
            metrics.mqtt_publishes.labels(topic_class=klass).inc()
        text = generate_latest(metrics.registry).decode("utf-8")
        for klass in ("aircraft", "summary", "alert", "status"):
            assert f'adsb_mqtt_publishes_total{{topic_class="{klass}"}} 1.0' in text

    def test_aircraft_active_per_band(self, metrics: MetricsRegistry) -> None:
        metrics.aircraft_active.labels(band="1090").set(42)
        metrics.aircraft_active.labels(band="978").set(7)
        text = generate_latest(metrics.registry).decode("utf-8")
        assert 'adsb_aircraft_active{band="1090"} 42.0' in text
        assert 'adsb_aircraft_active{band="978"} 7.0' in text

    def test_consecutive_failures_resets(self, metrics: MetricsRegistry) -> None:
        # Pattern the receiver health code will use: inc on each fail,
        # set to 0 on success.
        m = metrics.receiver_consecutive_failures.labels(receiver="rx-home")
        m.inc()
        m.inc()
        assert _value_of(metrics, "adsb_receiver_consecutive_failures") == 2.0
        m.set(0)
        assert _value_of(metrics, "adsb_receiver_consecutive_failures") == 0.0

    def test_poll_duration_observed(self, metrics: MetricsRegistry) -> None:
        # Histogram: observe a few values, verify count + sum recorded.
        h = metrics.receiver_poll_duration.labels(receiver="rx-home")
        h.observe(0.05)
        h.observe(0.10)
        h.observe(0.20)
        text = generate_latest(metrics.registry).decode("utf-8")
        assert 'adsb_receiver_poll_duration_seconds_count{receiver="rx-home"} 3.0' in text
        assert 'adsb_receiver_poll_duration_seconds_sum{receiver="rx-home"} 0.35' in text

    def test_mqtt_connected_gauge_toggles(self, metrics: MetricsRegistry) -> None:
        metrics.mqtt_connected.set(1)
        assert _value_of(metrics, "adsb_mqtt_connected") == 1.0
        metrics.mqtt_connected.set(0)
        assert _value_of(metrics, "adsb_mqtt_connected") == 0.0


# ---------------------------------------------------------------------------
# HTTP server lifecycle (mocked — actual port binding is integration only)
# ---------------------------------------------------------------------------


class TestStartServer:
    def test_calls_prometheus_start_http_server_with_defaults(
        self, metrics: MetricsRegistry
    ) -> None:
        # Defaults: localhost, 9090. Anyone exposing publicly does so
        # explicitly via config.
        with patch("ha_airspace.metrics.start_http_server") as mock_start:
            metrics.start_server()
            mock_start.assert_called_once_with(9090, addr="127.0.0.1", registry=metrics.registry)

    def test_passes_through_custom_port_and_addr(self, metrics: MetricsRegistry) -> None:
        with patch("ha_airspace.metrics.start_http_server") as mock_start:
            metrics.start_server(port=8000, addr="0.0.0.0")
            mock_start.assert_called_once_with(8000, addr="0.0.0.0", registry=metrics.registry)

    def test_idempotent_subsequent_calls_no_op(self, metrics: MetricsRegistry) -> None:
        # Safe to wire into restart paths without bookkeeping.
        with patch("ha_airspace.metrics.start_http_server") as mock_start:
            metrics.start_server()
            metrics.start_server()
            metrics.start_server(port=9999)  # different args, still skipped
            assert mock_start.call_count == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _value_of(metrics: MetricsRegistry, sample_name: str) -> float:
    """Return the first matching sample value for a given metric name.

    Walks the registry's collected samples directly. Less brittle than
    parsing the text exposition for single-value gauges/counters.
    """
    for collector in metrics.registry.collect():
        for sample in collector.samples:
            if sample.name == sample_name:
                return float(sample.value)
    raise AssertionError(f"no sample named {sample_name!r} in registry")
