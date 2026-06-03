"""Tests for the ReceiverSource ABC and shared fail-fast / health logic.

The base class is abstract, so tests use a programmable ``StubReceiver``
that lets each test queue success or failure for the next ``fetch()``
call. Keeps the base-class semantics isolated from any wire-format
parsing — those live in test_receivers_parse.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from ha_airspace.metrics import MetricsRegistry
from ha_airspace.models import AircraftObservation, ReceiverLocation
from ha_airspace.receivers.base import FetchError, ReceiverSource


class StubReceiver(ReceiverSource):
    """Programmable test double for the abstract base.

    Each test calls ``queue_success(...)`` or ``queue_failure(...)``
    before each ``fetch()``. The next ``_do_fetch()`` returns or raises
    accordingly. Lets us exercise base-class behavior without any
    real I/O.
    """

    def __init__(
        self, name: str = "test", band: str = "1090", *, metrics: MetricsRegistry | None = None
    ) -> None:
        super().__init__(name, band, metrics=metrics)
        self._next_observations: list[AircraftObservation] = []
        self._next_messages_per_sec: float | None = None
        self._next_failure: FetchError | None = None
        self._next_unexpected: Exception | None = None

    def queue_success(
        self,
        observations: list[AircraftObservation] | None = None,
        *,
        messages_per_sec: float | None = None,
    ) -> None:
        self._next_observations = observations or []
        self._next_messages_per_sec = messages_per_sec
        self._next_failure = None
        self._next_unexpected = None

    def queue_failure(self, exc: FetchError) -> None:
        self._next_failure = exc
        self._next_unexpected = None

    def queue_unexpected(self, exc: Exception) -> None:
        """Queue a non-FetchError to verify it propagates instead of
        being swallowed."""
        self._next_unexpected = exc
        self._next_failure = None

    async def _do_fetch(
        self,
    ) -> tuple[list[AircraftObservation], float | None]:
        if self._next_unexpected is not None:
            raise self._next_unexpected
        if self._next_failure is not None:
            raise self._next_failure
        return self._next_observations, self._next_messages_per_sec

    async def location(self) -> ReceiverLocation | None:
        return None


def _make_observation(name: str = "test", band: str = "1090") -> AircraftObservation:
    return AircraftObservation(
        hex="ae0001",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        seen_by=name,
        band=band,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_name_and_band_stored(self) -> None:
        rx = StubReceiver(name="rx-home", band="1090")
        assert rx.name == "rx-home"
        assert rx.band == "1090"

    async def test_initial_health_shape(self) -> None:
        rx = StubReceiver()
        h = await rx.health()
        assert h["online"] is True
        assert h["last_success"] is None
        assert h["consecutive_failures"] == 0
        assert h["aircraft_count"] == 0
        assert h["messages_per_sec"] == 0.0


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestSuccessPath:
    async def test_fetch_returns_observations(self) -> None:
        rx = StubReceiver()
        rx.queue_success([_make_observation()])
        result = await rx.fetch()
        assert len(result) == 1

    async def test_health_after_success(self) -> None:
        rx = StubReceiver()
        rx.queue_success([_make_observation(), _make_observation()], messages_per_sec=42.5)
        await rx.fetch()
        h = await rx.health()
        assert h["online"] is True
        assert h["consecutive_failures"] == 0
        assert h["aircraft_count"] == 2
        assert h["messages_per_sec"] == 42.5
        assert isinstance(h["last_success"], datetime)

    async def test_messages_per_sec_defaults_to_zero_when_not_provided(self) -> None:
        rx = StubReceiver()
        rx.queue_success([_make_observation()])  # no messages_per_sec
        await rx.fetch()
        h = await rx.health()
        assert h["messages_per_sec"] == 0.0

    async def test_success_after_failures_resets_counter(self) -> None:
        rx = StubReceiver()
        rx.queue_failure(FetchError("blip"))
        await rx.fetch()
        rx.queue_failure(FetchError("blip"))
        await rx.fetch()
        # Now succeed.
        rx.queue_success([_make_observation()])
        await rx.fetch()
        h = await rx.health()
        assert h["consecutive_failures"] == 0
        assert h["online"] is True


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


class TestFailurePath:
    async def test_fetch_returns_empty_on_fetch_error(self) -> None:
        rx = StubReceiver()
        rx.queue_failure(FetchError("timeout"))
        result = await rx.fetch()
        assert result == []

    async def test_consecutive_failures_increment(self) -> None:
        rx = StubReceiver()
        for _ in range(2):
            rx.queue_failure(FetchError("boom"))
            await rx.fetch()
        h = await rx.health()
        assert h["consecutive_failures"] == 2
        assert h["online"] is True  # still under threshold (3)

    async def test_unhealthy_after_three_consecutive_failures(self) -> None:
        # Locked threshold from /plan-eng-review D2.
        rx = StubReceiver()
        for _ in range(3):
            rx.queue_failure(FetchError("boom"))
            await rx.fetch()
        h = await rx.health()
        assert h["consecutive_failures"] == 3
        assert h["online"] is False

    async def test_unhealthy_persists_until_success(self) -> None:
        rx = StubReceiver()
        for _ in range(5):
            rx.queue_failure(FetchError("boom"))
            await rx.fetch()
        assert (await rx.health())["online"] is False
        # One success flips it back.
        rx.queue_success([_make_observation()])
        await rx.fetch()
        assert (await rx.health())["online"] is True

    async def test_non_fetcherror_propagates(self) -> None:
        # If a subclass lets a non-FetchError through, that is a real
        # bug — should not be silently treated as a transient failure.
        rx = StubReceiver()
        rx.queue_unexpected(RuntimeError("genuine bug"))
        with pytest.raises(RuntimeError, match="genuine bug"):
            await rx.fetch()
        # Health should NOT have been updated by this path.
        h = await rx.health()
        assert h["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# Metrics integration
# ---------------------------------------------------------------------------


class TestMetricsIntegration:
    @pytest.fixture
    def metrics(self) -> MetricsRegistry:
        return MetricsRegistry(registry=CollectorRegistry())

    async def test_success_increments_ok_counter_and_sets_gauges(
        self, metrics: MetricsRegistry
    ) -> None:
        rx = StubReceiver(name="rx-home", metrics=metrics)
        rx.queue_success(
            [_make_observation(name="rx-home"), _make_observation(name="rx-home")],
            messages_per_sec=42.0,
        )
        await rx.fetch()

        ok = metrics.receiver_polls.labels(receiver="rx-home", status="ok")
        assert _counter_value(ok) == 1.0

        visible = metrics.receiver_aircraft_visible.labels(receiver="rx-home")
        assert _gauge_value(visible) == 2.0

        mps = metrics.receiver_messages_per_second.labels(receiver="rx-home")
        assert _gauge_value(mps) == 42.0

        cf = metrics.receiver_consecutive_failures.labels(receiver="rx-home")
        assert _gauge_value(cf) == 0.0

    async def test_failure_increments_fail_counter_and_failure_gauge(
        self, metrics: MetricsRegistry
    ) -> None:
        rx = StubReceiver(name="rx-home", metrics=metrics)
        rx.queue_failure(FetchError("timeout"))
        await rx.fetch()
        rx.queue_failure(FetchError("timeout"))
        await rx.fetch()

        fail = metrics.receiver_polls.labels(receiver="rx-home", status="fail")
        assert _counter_value(fail) == 2.0

        cf = metrics.receiver_consecutive_failures.labels(receiver="rx-home")
        assert _gauge_value(cf) == 2.0

    async def test_metrics_optional(self) -> None:
        # Receiver constructed without metrics should still work — no
        # AttributeError on success or failure path.
        rx = StubReceiver(name="rx-home")  # metrics=None
        rx.queue_success([_make_observation(name="rx-home")])
        await rx.fetch()  # should not raise
        rx.queue_failure(FetchError("blip"))
        await rx.fetch()  # should not raise


# ---------------------------------------------------------------------------
# Helpers — read prometheus_client metric values without parsing exposition.
#
# Counter.labels() and Gauge.labels() return child metric objects; their
# canonical-but-private accessor is ``._value.get()``. ``Any``-typed args
# satisfy mypy without relaxing the rest of the test file.
# ---------------------------------------------------------------------------


def _counter_value(counter: Any) -> float:
    return float(counter._value.get())


def _gauge_value(gauge: Any) -> float:
    return float(gauge._value.get())
