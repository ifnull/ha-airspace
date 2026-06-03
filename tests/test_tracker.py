"""Tests for ha_airspace.tracker.AircraftTracker.

Drives the tracker with a FakePublisher (records every call) and an
injected clock (a mutable datetime holder), so lifecycle timing is
deterministic and no MQTT / network is touched.

Cover:
  * Ingest: new hex creates state; repeat hex updates canonical latest-wins.
  * Geometry: distance/bearing computed per watchpoint; cleared when no position.
  * Lifecycle: ACTIVE/STALE republish; PURGED clears retained + drops state.
  * Nearest: closest-to-primary wins; positionless skipped; None when empty.
  * Primary watchpoint: "home" preferred, else first.
  * Summary: count + nearest published every poll.
  * Metrics: active gauge tracks count by band, zeroes emptied bands.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from ha_airspace.config import EnrichmentConfig, FlagConfig
from ha_airspace.enrichment import Enricher
from ha_airspace.metrics import MetricsRegistry
from ha_airspace.models import AircraftObservation, AircraftState, Watchpoint
from ha_airspace.tracker import AircraftTracker

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePublisher:
    """Records publisher calls without exercising MQTT.

    The tracker only ever calls these three methods; we record args so
    tests can assert on what was published, purged, and summarized.
    """

    def __init__(self) -> None:
        self.published: list[AircraftState] = []
        self.purged: list[str] = []
        self.summaries: list[dict[str, Any]] = []

    async def publish_aircraft(self, state: AircraftState) -> bool:
        self.published.append(state)
        return True

    async def purge_aircraft(self, hex_code: str) -> None:
        self.purged.append(hex_code)

    async def publish_summary(
        self,
        *,
        count: int,
        nearest: AircraftState | None,
        count_by_flag: dict[str, int] | None = None,
    ) -> bool:
        self.summaries.append({"count": count, "nearest": nearest, "count_by_flag": count_by_flag})
        return True


class Clock:
    """Mutable datetime holder used as the tracker's injected clock."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# Watchpoint at Austin-ish; aircraft coords below are relative to it.
_HOME = Watchpoint(name="home", lat=30.33, lon=-97.99)
_OFFICE = Watchpoint(name="office", lat=30.40, lon=-97.70)


def _obs(
    hex_code: str = "ae0001",
    *,
    at: datetime = _T0,
    lat: float | None = 30.40,
    lon: float | None = -97.90,
    band: str = "1090",
    seen_by: str = "rx-home",
    flight: str | None = "RCH171",
    category: str | None = None,
    squawk: str | None = None,
) -> AircraftObservation:
    return AircraftObservation(
        hex=hex_code,
        observed_at=at,
        seen_by=seen_by,
        band=band,
        flight=flight,
        lat=lat,
        lon=lon,
        alt_baro_ft=35000,
        category=category,
        squawk=squawk,
    )


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def clock() -> Clock:
    return Clock(_T0)


def _make_tracker(
    publisher: FakePublisher,
    clock: Clock,
    *,
    watchpoints: list[Watchpoint] | None = None,
    metrics: MetricsRegistry | None = None,
) -> AircraftTracker:
    return AircraftTracker(
        publisher,  # type: ignore[arg-type]
        watchpoints if watchpoints is not None else [_HOME],
        metrics=metrics,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class TestIngest:
    async def test_new_hex_creates_state(self, publisher: FakePublisher, clock: Clock) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001")])
        assert tracker.tracked_count == 1
        assert publisher.published[0].hex == "ae0001"

    async def test_repeat_hex_updates_canonical_latest_wins(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001", flight="RCH171")])
        clock.advance(1.0)
        await tracker.process_poll([_obs("ae0001", at=clock.now, flight="RCH999")])
        # Still one aircraft; canonical reflects the newer observation.
        assert tracker.tracked_count == 1
        latest = publisher.published[-1]
        assert latest.canonical.flight == "RCH999"
        assert latest.last_seen == clock.now

    async def test_multiple_distinct_hexes_tracked(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001"), _obs("ae0002"), _obs("ae0003")])
        assert tracker.tracked_count == 3


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class TestGeometry:
    async def test_distance_and_bearing_per_watchpoint(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock, watchpoints=[_HOME, _OFFICE])
        await tracker.process_poll([_obs("ae0001", lat=30.40, lon=-97.90)])
        state = publisher.published[-1]
        assert set(state.distance_to) == {"home", "office"}
        assert set(state.bearing_to) == {"home", "office"}
        # Aircraft NE of home: positive distance, bearing in the NE quadrant.
        assert state.distance_to["home"] > 0
        assert 0 < state.bearing_to["home"] < 90

    async def test_positionless_observation_clears_geometry(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        # First a positioned obs, then the same hex with no position.
        await tracker.process_poll([_obs("ae0001", lat=30.40, lon=-97.90)])
        assert publisher.published[-1].distance_to
        clock.advance(1.0)
        await tracker.process_poll([_obs("ae0001", at=clock.now, lat=None, lon=None)])
        assert publisher.published[-1].distance_to == {}
        assert publisher.published[-1].bearing_to == {}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_active_republishes_each_poll(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001", at=clock.now)])
        clock.advance(1.0)
        await tracker.process_poll([_obs("ae0001", at=clock.now)])
        # Published on both polls; still active.
        assert sum(1 for s in publisher.published if s.hex == "ae0001") == 2
        assert not publisher.purged

    async def test_stale_aircraft_still_republished(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001", at=clock.now)])
        # No new observation; advance past stale (5s) but not expire (60s).
        clock.advance(10.0)
        await tracker.process_poll([])
        # Still tracked and republished (keeps HA from blinking), not purged.
        assert tracker.tracked_count == 1
        assert "ae0001" not in publisher.purged
        assert publisher.published[-1].hex == "ae0001"

    async def test_expired_aircraft_purged_and_dropped(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001", at=clock.now)])
        clock.advance(61.0)  # past expire_after_s
        await tracker.process_poll([])
        assert tracker.tracked_count == 0
        assert publisher.purged == ["ae0001"]

    async def test_purged_aircraft_not_republished(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001", at=clock.now)])
        published_before = len(publisher.published)
        clock.advance(61.0)
        await tracker.process_poll([])
        # The expired aircraft was purged, not published again.
        assert not any(s.hex == "ae0001" for s in publisher.published[published_before:])

    async def test_reappearing_aircraft_after_purge_is_fresh(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001", at=clock.now)])
        clock.advance(61.0)
        await tracker.process_poll([])  # purges ae0001
        clock.advance(1.0)
        await tracker.process_poll([_obs("ae0001", at=clock.now)])  # comes back
        assert tracker.tracked_count == 1
        assert publisher.published[-1].first_seen == clock.now


# ---------------------------------------------------------------------------
# Nearest
# ---------------------------------------------------------------------------


class TestNearest:
    async def test_closest_to_primary_is_nearest(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        # ae0002 is closer to home (30.33,-97.99) than ae0001.
        far = _obs("ae0001", lat=31.50, lon=-97.00)
        near = _obs("ae0002", lat=30.34, lon=-97.98)
        await tracker.process_poll([far, near])
        nearest = publisher.summaries[-1]["nearest"]
        assert nearest is not None
        assert nearest.hex == "ae0002"

    async def test_positionless_aircraft_skipped_for_nearest(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll(
            [_obs("ae0001", lat=None, lon=None), _obs("ae0002", lat=30.34, lon=-97.98)]
        )
        nearest = publisher.summaries[-1]["nearest"]
        assert nearest is not None
        assert nearest.hex == "ae0002"

    async def test_no_positioned_aircraft_yields_none(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001", lat=None, lon=None)])
        assert publisher.summaries[-1]["nearest"] is None

    async def test_empty_airspace_yields_none(self, publisher: FakePublisher, clock: Clock) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([])
        assert publisher.summaries[-1] == {
            "count": 0,
            "nearest": None,
            "count_by_flag": {},
        }


# ---------------------------------------------------------------------------
# Primary watchpoint selection
# ---------------------------------------------------------------------------


class TestPrimaryWatchpoint:
    async def test_home_preferred_when_present(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        # office listed first, but home is the primary for "nearest".
        tracker = _make_tracker(publisher, clock, watchpoints=[_OFFICE, _HOME])
        near_home = _obs("ae0001", lat=30.34, lon=-97.98)
        await tracker.process_poll([near_home])
        nearest = publisher.summaries[-1]["nearest"]
        assert nearest is not None
        assert nearest.hex == "ae0001"
        # distance_to keyed by both watchpoint names confirms both computed.
        assert set(nearest.distance_to) == {"home", "office"}

    async def test_first_watchpoint_used_when_no_home(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        wp_a = Watchpoint(name="alpha", lat=30.33, lon=-97.99)
        wp_b = Watchpoint(name="bravo", lat=10.0, lon=10.0)
        tracker = _make_tracker(publisher, clock, watchpoints=[wp_a, wp_b])
        await tracker.process_poll([_obs("ae0001", lat=30.34, lon=-97.98)])
        # Nearest measured to alpha (first); it resolves regardless, but the
        # primary-key path must not raise when no "home" exists.
        assert publisher.summaries[-1]["nearest"] is not None

    def test_empty_watchpoints_rejected(self, publisher: FakePublisher, clock: Clock) -> None:
        with pytest.raises(ValueError, match="at least one watchpoint"):
            AircraftTracker(publisher, [], clock=clock)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class TestSummary:
    async def test_summary_published_every_poll(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001")])
        clock.advance(1.0)
        await tracker.process_poll([_obs("ae0001", at=clock.now), _obs("ae0002", at=clock.now)])
        assert len(publisher.summaries) == 2
        assert publisher.summaries[-1]["count"] == 2

    async def test_count_by_flag_empty_without_enricher(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001"), _obs("ae0002")])
        # No enricher -> no flags -> empty count_by_flag.
        assert publisher.summaries[-1]["count_by_flag"] == {}

    async def test_count_by_flag_aggregates_across_aircraft(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        enricher = Enricher(
            EnrichmentConfig(
                flags={
                    "heavy": FlagConfig(categories=["A3"]),
                    "emergency": FlagConfig(squawks=["7700"]),
                }
            )
        )
        tracker = AircraftTracker(
            publisher,  # type: ignore[arg-type]
            [_HOME],
            enricher=enricher,
            clock=clock,
        )
        # Two A3 (heavy); one of them also squawking 7700 (emergency); one A1.
        a = _obs("ae0001", category="A3")
        b = _obs("ae0002", category="A3", squawk="7700")
        c = _obs("ae0003", category="A1")
        await tracker.process_poll([a, b, c])

        counts = publisher.summaries[-1]["count_by_flag"]
        assert counts == {"heavy": 2, "emergency": 1}

    async def test_count_by_flag_drops_emptied_flags(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        enricher = Enricher(EnrichmentConfig(flags={"emergency": FlagConfig(squawks=["7700"])}))
        tracker = AircraftTracker(
            publisher,  # type: ignore[arg-type]
            [_HOME],
            enricher=enricher,
            clock=clock,
        )
        await tracker.process_poll([_obs("ae0001", at=clock.now, squawk="7700")])
        assert publisher.summaries[-1]["count_by_flag"] == {"emergency": 1}
        # Squawk cleared next poll -> flag drops -> key omitted entirely.
        clock.advance(1.0)
        await tracker.process_poll([_obs("ae0001", at=clock.now, squawk=None)])
        assert publisher.summaries[-1]["count_by_flag"] == {}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    async def test_active_gauge_tracks_count_by_band(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        metrics = MetricsRegistry(registry=CollectorRegistry())
        tracker = _make_tracker(publisher, clock, metrics=metrics)
        await tracker.process_poll([_obs("ae0001", band="1090"), _obs("ae0002", band="978")])
        assert _gauge(metrics, "1090") == 1.0
        assert _gauge(metrics, "978") == 1.0

    async def test_emptied_band_gauge_zeroed(self, publisher: FakePublisher, clock: Clock) -> None:
        metrics = MetricsRegistry(registry=CollectorRegistry())
        tracker = _make_tracker(publisher, clock, metrics=metrics)
        await tracker.process_poll([_obs("ae0001", band="1090", at=clock.now)])
        assert _gauge(metrics, "1090") == 1.0
        clock.advance(61.0)
        await tracker.process_poll([])  # ae0001 expires
        assert _gauge(metrics, "1090") == 0.0


def _gauge(metrics: MetricsRegistry, band: str) -> float:
    return metrics.aircraft_active.labels(band=band)._value.get()
