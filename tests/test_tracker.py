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

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from ha_airspace.alerts import AlertEvaluator
from ha_airspace.config import (
    AlertRule,
    AlertsConfig,
    EnrichmentConfig,
    FlagConfig,
    JournalConfig,
    MatchBlock,
    OrbitConfig,
)
from ha_airspace.enrichment import Enricher
from ha_airspace.journal import Journal
from ha_airspace.metrics import MetricsRegistry
from ha_airspace.models import AircraftObservation, AircraftState, DroneInfo, Watchpoint
from ha_airspace.mqtt.payloads import PhotoPayload
from ha_airspace.orbit import OrbitDetector
from ha_airspace.tracker import _FLAG_FEED_MAX, AircraftTracker

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
        self.alerts: list[tuple[str, str]] = []  # (rule, hex) ENTER publishes
        self.alert_photos: list[tuple[str, Any]] = []  # (rule, photo) on ENTER
        self.alerts_cleared: list[tuple[str, str]] = []  # (rule, hex) EXIT clears
        self.alert_active: list[tuple[str, bool]] = []  # (rule, active)
        self.drones_published: list[AircraftState] = []
        self.drones_purged: list[str] = []
        self.drone_summaries: list[dict[str, Any]] = []

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
        flag_feeds: dict[str, Any] | None = None,
    ) -> bool:
        self.summaries.append(
            {
                "count": count,
                "nearest": nearest,
                "count_by_flag": count_by_flag,
                "flag_feeds": flag_feeds,
            }
        )
        return True

    async def publish_alert(self, rule: str, state: AircraftState, *, photo: Any = None) -> None:
        self.alerts.append((rule, state.hex))
        self.alert_photos.append((rule, photo))

    async def clear_alert(self, rule: str, hex_code: str) -> None:
        self.alerts_cleared.append((rule, hex_code))

    async def publish_alert_active(self, rule: str, *, active: bool) -> None:
        self.alert_active.append((rule, active))

    async def publish_drone(self, state: AircraftState) -> bool:
        self.drones_published.append(state)
        return True

    async def purge_drone(self, track_id: str) -> None:
        self.drones_purged.append(track_id)

    async def publish_drone_summary(self, *, count: int, nearest: AircraftState | None) -> bool:
        self.drone_summaries.append({"count": count, "nearest": nearest})
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
            "flag_feeds": {},
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


class TestFlagFeeds:
    def _tracker(
        self, publisher: FakePublisher, clock: Clock, *, feed_flags: list[str]
    ) -> AircraftTracker:
        enricher = Enricher(
            EnrichmentConfig(
                flags={
                    "heavy": FlagConfig(categories=["A3"]),
                    "emergency": FlagConfig(squawks=["7700"]),
                }
            )
        )
        return AircraftTracker(
            publisher,  # type: ignore[arg-type]
            [_HOME],
            enricher=enricher,
            feed_flags=feed_flags,
            clock=clock,
        )

    async def test_no_feed_flags_publishes_no_feeds(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock, feed_flags=[])
        await tracker.process_poll([_obs("ae0001", category="A3")])
        assert publisher.summaries[-1]["flag_feeds"] == {}

    async def test_configured_flag_always_present_even_when_empty(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock, feed_flags=["heavy", "emergency"])
        await tracker.process_poll([_obs("ae0001", category="A1")])  # matches neither
        feeds = publisher.summaries[-1]["flag_feeds"]
        assert set(feeds) == {"heavy", "emergency"}
        assert feeds["heavy"].count == 0
        assert feeds["heavy"].aircraft == []

    async def test_feed_lists_matching_aircraft_with_detail(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock, feed_flags=["heavy"])
        await tracker.process_poll([_obs("ae0001", category="A3", squawk="1200")])
        feed = publisher.summaries[-1]["flag_feeds"]["heavy"]
        assert feed.count == 1
        (row,) = feed.aircraft
        assert row.hex == "ae0001"
        assert row.aircraft_type is None  # no DB; type unset
        assert row.alt_baro_ft == 35000
        assert row.squawk == "1200"
        assert "heavy" in row.flags
        assert row.distance_nm is not None
        assert row.distance_nm > 0
        assert feed.watchpoint == "home"

    async def test_feed_sorted_nearest_first(self, publisher: FakePublisher, clock: Clock) -> None:
        tracker = self._tracker(publisher, clock, feed_flags=["heavy"])
        # Two heavies: ae0002 is closer to home than ae0001.
        far = _obs("ae0001", category="A3", lat=31.50, lon=-97.99)
        near = _obs("ae0002", category="A3", lat=30.40, lon=-97.99)
        await tracker.process_poll([far, near])
        rows = publisher.summaries[-1]["flag_feeds"]["heavy"].aircraft
        assert [r.hex for r in rows] == ["ae0002", "ae0001"]

    async def test_feed_caps_list_but_counts_all(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock, feed_flags=["heavy"])
        obs = [_obs(f"ae{i:04x}", category="A3") for i in range(_FLAG_FEED_MAX + 5)]
        await tracker.process_poll(obs)
        feed = publisher.summaries[-1]["flag_feeds"]["heavy"]
        assert feed.count == _FLAG_FEED_MAX + 5
        assert len(feed.aircraft) == _FLAG_FEED_MAX


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


# ---------------------------------------------------------------------------
# Alerts (slice 3) — driven through the tracker
# ---------------------------------------------------------------------------


class TestAlerts:
    def _tracker_with_alert(self, publisher: FakePublisher, clock: Clock) -> AircraftTracker:
        enricher = Enricher(EnrichmentConfig(flags={"heavy": FlagConfig(categories=["A3"])}))
        alerts = AlertEvaluator(
            AlertsConfig(rules=[AlertRule(name="heavy_close", match=MatchBlock(flags=["heavy"]))]),
            elevation_m_for=lambda _n: None,
            clock=clock,
        )
        return AircraftTracker(
            publisher,  # type: ignore[arg-type]
            [_HOME],
            enricher=enricher,
            alerts=alerts,
            clock=clock,
        )

    async def test_enter_publishes_alert_and_active(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker_with_alert(publisher, clock)
        await tracker.process_poll([_obs("ae0001", category="A3")])
        assert ("heavy_close", "ae0001") in publisher.alerts
        assert ("heavy_close", True) in publisher.alert_active

    async def test_exit_clears_alert_when_aircraft_expires(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker_with_alert(publisher, clock)
        await tracker.process_poll([_obs("ae0001", at=clock.now, category="A3")])
        clock.advance(61.0)
        await tracker.process_poll([])  # ae0001 expires -> alert EXIT
        assert ("heavy_close", "ae0001") in publisher.alerts_cleared
        assert ("heavy_close", False) in publisher.alert_active

    async def test_no_alerts_without_evaluator(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)  # no alerts
        await tracker.process_poll([_obs("ae0001", category="A3")])
        assert publisher.alerts == []
        assert publisher.alert_active == []


# ---------------------------------------------------------------------------
# Drone routing (Phase 3 — Remote ID)
# ---------------------------------------------------------------------------


def _drone_obs(track_id: str = "Drone1", *, at: datetime = _T0) -> AircraftObservation:
    return AircraftObservation(
        track_id=track_id,
        hex=None,
        non_icao=True,
        observed_at=at,
        seen_by="dump3411",
        band="remoteid",
        lat=30.34,
        lon=-97.98,
        alt_geom_ft=400,
        drone=DroneInfo(id_type="serial", agl_ft=300.0, operator_lat=30.33, operator_lon=-97.99),
    )


class TestDroneRouting:
    def _tracker(self, publisher: FakePublisher, clock: Clock) -> AircraftTracker:
        return AircraftTracker(
            publisher,  # type: ignore[arg-type]
            [_HOME],
            has_drone_source=True,
            clock=clock,
        )

    async def test_drone_publishes_to_drone_topic_not_aircraft(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock)
        await tracker.process_poll([_drone_obs("Drone1")])
        assert [s.track_id for s in publisher.drones_published] == ["Drone1"]
        assert publisher.published == []  # not on the aircraft path

    async def test_aircraft_and_drone_routed_separately(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock)
        await tracker.process_poll([_obs("ae0001"), _drone_obs("Drone1")])
        assert [s.hex for s in publisher.published] == ["ae0001"]
        assert [s.track_id for s in publisher.drones_published] == ["Drone1"]
        # Separate summaries: aircraft count excludes the drone.
        assert publisher.summaries[-1]["count"] == 1
        assert publisher.drone_summaries[-1]["count"] == 1

    async def test_drone_summary_suppressed_without_source(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        # No has_drone_source -> no drone summary even if a drone slips in.
        tracker = AircraftTracker(publisher, [_HOME], clock=clock)  # type: ignore[arg-type]
        await tracker.process_poll([_drone_obs("Drone1")])
        assert publisher.drone_summaries == []

    async def test_expired_drone_purged_via_drone_path(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock)
        await tracker.process_poll([_drone_obs("Drone1", at=clock.now)])
        clock.advance(61.0)
        await tracker.process_poll([])
        assert "Drone1" in publisher.drones_purged
        assert "Drone1" not in publisher.purged


# ---------------------------------------------------------------------------
# Journal event history (Phase 2b slice 2) — flag/alert transitions recorded
# ---------------------------------------------------------------------------


class TestJournalEvents:
    async def _journal(self, tmp_path: Path) -> Journal:
        j = Journal(JournalConfig(path=str(tmp_path / "journal.db")))
        await j.open()
        await j.warm_load()
        return j

    @staticmethod
    async def _events(j: Journal) -> list[tuple[str, str, str | None]]:
        # record_event only buffers; flush before reading.
        await asyncio.to_thread(j._flush_sync)
        return [(tid, kind, detail) for tid, kind, detail, _at in await j.load_events()]

    def _tracker(
        self,
        publisher: FakePublisher,
        clock: Clock,
        journal: Journal,
        *,
        alerts: AlertEvaluator | None = None,
    ) -> AircraftTracker:
        enricher = Enricher(EnrichmentConfig(flags={"heavy": FlagConfig(categories=["A3"])}))
        return AircraftTracker(
            publisher,  # type: ignore[arg-type]
            [_HOME],
            enricher=enricher,
            alerts=alerts,
            clock=clock,
            journal=journal,
        )

    async def test_flag_enter_recorded(
        self, publisher: FakePublisher, clock: Clock, tmp_path: Path
    ) -> None:
        journal = await self._journal(tmp_path)
        tracker = self._tracker(publisher, clock, journal)
        await tracker.process_poll([_obs("ae0001", category="A3")])  # A3 -> heavy
        assert ("ae0001", "flag_enter", "heavy") in await self._events(journal)
        await journal.close()

    async def test_flag_exit_recorded_when_flag_drops(
        self, publisher: FakePublisher, clock: Clock, tmp_path: Path
    ) -> None:
        journal = await self._journal(tmp_path)
        tracker = self._tracker(publisher, clock, journal)
        await tracker.process_poll([_obs("ae0001", at=clock.now, category="A3")])
        clock.advance(1.0)
        # Same track, no longer A3 -> heavy flag drops -> flag_exit.
        await tracker.process_poll([_obs("ae0001", at=clock.now, category="A1")])
        events = await self._events(journal)
        assert ("ae0001", "flag_enter", "heavy") in events
        assert ("ae0001", "flag_exit", "heavy") in events
        await journal.close()

    async def test_unchanged_flags_record_no_event(
        self, publisher: FakePublisher, clock: Clock, tmp_path: Path
    ) -> None:
        journal = await self._journal(tmp_path)
        tracker = self._tracker(publisher, clock, journal)
        await tracker.process_poll([_obs("ae0001", at=clock.now, category="A3")])
        clock.advance(1.0)
        await tracker.process_poll([_obs("ae0001", at=clock.now, category="A3")])
        # The flag set is identical across polls -> exactly one flag_enter, no churn.
        kinds = [(k, d) for _t, k, d in await self._events(journal)]
        assert kinds == [("flag_enter", "heavy")]
        await journal.close()

    async def test_flag_exit_recorded_on_purge(
        self, publisher: FakePublisher, clock: Clock, tmp_path: Path
    ) -> None:
        journal = await self._journal(tmp_path)
        tracker = self._tracker(publisher, clock, journal)
        await tracker.process_poll([_obs("ae0001", at=clock.now, category="A3")])
        clock.advance(61.0)
        await tracker.process_poll([])  # ae0001 expires + purges
        assert ("ae0001", "flag_exit", "heavy") in await self._events(journal)
        await journal.close()

    async def test_alert_enter_and_exit_recorded(
        self, publisher: FakePublisher, clock: Clock, tmp_path: Path
    ) -> None:
        journal = await self._journal(tmp_path)
        alerts = AlertEvaluator(
            AlertsConfig(rules=[AlertRule(name="heavy_close", match=MatchBlock(flags=["heavy"]))]),
            elevation_m_for=lambda _n: None,
            clock=clock,
        )
        tracker = self._tracker(publisher, clock, journal, alerts=alerts)
        await tracker.process_poll([_obs("ae0001", at=clock.now, category="A3")])  # ENTER
        clock.advance(61.0)
        await tracker.process_poll([])  # expire -> alert EXIT
        events = await self._events(journal)
        assert ("ae0001", "alert_enter", "heavy_close") in events
        assert ("ae0001", "alert_exit", "heavy_close") in events
        await journal.close()

    async def test_no_journal_means_no_recording(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        # The prev-flags map is still maintained, but nothing is journaled and
        # no crash occurs on the None-journal path.
        enricher = Enricher(EnrichmentConfig(flags={"heavy": FlagConfig(categories=["A3"])}))
        tracker = AircraftTracker(
            publisher,  # type: ignore[arg-type]
            [_HOME],
            enricher=enricher,
            clock=clock,
        )
        await tracker.process_poll([_obs("ae0001", category="A3")])
        assert tracker.tracked_count == 1  # no exception, flags still tracked


# ---------------------------------------------------------------------------
# Photo enrichment on alerts (Phase 2c)
# ---------------------------------------------------------------------------


class _StubPhotos:
    """Records photo_for calls; returns a fixed photo (or None)."""

    def __init__(self, photo: PhotoPayload | None) -> None:
        self._photo = photo
        self.calls: list[str] = []

    async def photo_for(self, hex_code: str) -> PhotoPayload | None:
        self.calls.append(hex_code)
        return self._photo


class TestAlertPhotos:
    def _tracker(
        self, publisher: FakePublisher, clock: Clock, photos: _StubPhotos | None
    ) -> AircraftTracker:
        enricher = Enricher(EnrichmentConfig(flags={"heavy": FlagConfig(categories=["A3"])}))
        alerts = AlertEvaluator(
            AlertsConfig(rules=[AlertRule(name="heavy_close", match=MatchBlock(flags=["heavy"]))]),
            elevation_m_for=lambda _n: None,
            clock=clock,
        )
        return AircraftTracker(
            publisher,  # type: ignore[arg-type]
            [_HOME],
            enricher=enricher,
            alerts=alerts,
            photos=photos,  # type: ignore[arg-type]
            clock=clock,
        )

    async def test_photo_passed_to_publish_alert_on_enter(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        photo = PhotoPayload(thumbnail_url="https://t/img.jpg", photographer="Jane")
        stub = _StubPhotos(photo)
        tracker = self._tracker(publisher, clock, stub)
        await tracker.process_poll([_obs("ae0001", category="A3")])
        assert stub.calls == ["ae0001"]  # looked up by hex
        assert ("heavy_close", photo) in publisher.alert_photos

    async def test_no_enricher_means_photo_none(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock, None)
        await tracker.process_poll([_obs("ae0001", category="A3")])
        # Alert still fires; photo is just None.
        assert ("heavy_close", "ae0001") in publisher.alerts
        assert publisher.alert_photos == [("heavy_close", None)]

    async def test_miss_returns_none_but_alert_still_publishes(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        stub = _StubPhotos(None)  # no photo for this hex
        tracker = self._tracker(publisher, clock, stub)
        await tracker.process_poll([_obs("ae0001", category="A3")])
        assert stub.calls == ["ae0001"]
        assert ("heavy_close", None) in publisher.alert_photos


# ---------------------------------------------------------------------------
# Orbit detection (Phase 5) — derived "orbiting" flag, end to end
# ---------------------------------------------------------------------------


def _obs_heading(heading: float, at: datetime) -> AircraftObservation:
    return AircraftObservation(
        hex="ae0001",
        observed_at=at,
        seen_by="rx-home",
        band="1090",
        lat=30.40,
        lon=-97.90,
        alt_baro_ft=3000,
        track_deg=heading,
    )


class TestOrbitIntegration:
    def _tracker(self, publisher: FakePublisher, clock: Clock) -> AircraftTracker:
        return AircraftTracker(
            publisher,  # type: ignore[arg-type]
            [_HOME],
            orbit=OrbitDetector(OrbitConfig(enabled=True, window_s=120, min_turn_deg=180)),
            clock=clock,
        )

    async def _fly(self, tracker: AircraftTracker, clock: Clock, headings: list[float]) -> None:
        for i, h in enumerate(headings):
            t = _T0 + timedelta(seconds=i * 5)
            clock.now = t
            await tracker.process_poll([_obs_heading(h, t)])

    async def test_sustained_turn_flags_orbiting(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock)
        await self._fly(tracker, clock, [0, 90, 180])  # +180 cumulative >= threshold
        assert "orbiting" in publisher.published[-1].flags

    async def test_straight_flight_not_orbiting(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = self._tracker(publisher, clock)
        await self._fly(tracker, clock, [90, 90, 90, 90])
        assert "orbiting" not in publisher.published[-1].flags

    async def test_no_detector_no_orbiting(self, publisher: FakePublisher, clock: Clock) -> None:
        tracker = _make_tracker(publisher, clock)  # no orbit detector
        await self._fly(tracker, clock, [0, 90, 180, 270])
        assert "orbiting" not in publisher.published[-1].flags


# ---------------------------------------------------------------------------
# Predictive fields (Phase 5) — computed in the geometry pass
# ---------------------------------------------------------------------------


def _obs_kinematic(
    *,
    lat: float,
    lon: float,
    track_deg: float | None,
    ground_speed_kt: float | None,
    on_ground: bool = False,
    at: datetime = _T0,
) -> AircraftObservation:
    return AircraftObservation(
        hex="ae0001",
        observed_at=at,
        seen_by="rx-home",
        band="1090",
        lat=lat,
        lon=lon,
        alt_baro_ft=8000,
        track_deg=track_deg,
        ground_speed_kt=ground_speed_kt,
        on_ground=on_ground,
    )


class TestPrediction:
    async def test_approaching_sets_cpa_and_eta(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)  # primary watchpoint = _HOME
        # South of home, tracking north toward it.
        await tracker.process_poll(
            [_obs_kinematic(lat=29.83, lon=-97.99, track_deg=0.0, ground_speed_kt=300.0)]
        )
        s = publisher.published[-1]
        assert s.predicted_closest_approach_nm is not None
        assert s.predicted_closest_approach_nm < 1.0  # heading right at home
        assert s.predicted_eta_to_home_s is not None
        assert s.predicted_eta_to_home_s > 0

    async def test_departing_has_no_eta(self, publisher: FakePublisher, clock: Clock) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll(
            [_obs_kinematic(lat=29.83, lon=-97.99, track_deg=180.0, ground_speed_kt=300.0)]
        )
        s = publisher.published[-1]
        assert s.predicted_eta_to_home_s is None
        assert s.predicted_closest_approach_nm is not None  # current distance

    async def test_no_track_no_prediction(self, publisher: FakePublisher, clock: Clock) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll(
            [_obs_kinematic(lat=29.83, lon=-97.99, track_deg=None, ground_speed_kt=300.0)]
        )
        s = publisher.published[-1]
        assert s.predicted_closest_approach_nm is None
        assert s.predicted_eta_to_home_s is None

    async def test_too_slow_no_prediction(self, publisher: FakePublisher, clock: Clock) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll(
            [_obs_kinematic(lat=29.83, lon=-97.99, track_deg=0.0, ground_speed_kt=20.0)]
        )
        assert publisher.published[-1].predicted_closest_approach_nm is None

    async def test_on_ground_no_prediction(self, publisher: FakePublisher, clock: Clock) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll(
            [
                _obs_kinematic(
                    lat=29.83, lon=-97.99, track_deg=0.0, ground_speed_kt=300.0, on_ground=True
                )
            ]
        )
        assert publisher.published[-1].predicted_closest_approach_nm is None

    async def test_no_position_clears_prediction(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _make_tracker(publisher, clock)
        await tracker.process_poll(
            [_obs_kinematic(lat=29.83, lon=-97.99, track_deg=0.0, ground_speed_kt=300.0)]
        )
        clock.advance(1.0)
        # Same track, now no position -> prediction must clear.
        await tracker.process_poll(
            [
                AircraftObservation(
                    hex="ae0001",
                    observed_at=clock.now,
                    seen_by="rx-home",
                    band="1090",
                    lat=None,
                    lon=None,
                    track_deg=0.0,
                    ground_speed_kt=300.0,
                )
            ]
        )
        s = publisher.published[-1]
        assert s.predicted_closest_approach_nm is None
        assert s.predicted_eta_to_home_s is None


# ---------------------------------------------------------------------------
# Drone FAA registry enrichment (Phase 5+)
# ---------------------------------------------------------------------------


class _StubRegistry:
    """Records lookups; returns fixed info (or None)."""

    def __init__(self, info: dict[str, Any] | None) -> None:
        self._info = info
        self.calls: list[str] = []

    async def lookup(self, serial: str) -> dict[str, Any] | None:
        self.calls.append(serial)
        return self._info


def _tracker_with_registry(
    publisher: FakePublisher, clock: Clock, registry: _StubRegistry | None
) -> AircraftTracker:
    return AircraftTracker(
        publisher,  # type: ignore[arg-type]
        [_HOME],
        drone_registry=registry,  # type: ignore[arg-type]
        has_drone_source=True,
        clock=clock,
    )


class TestDroneRegistry:
    async def test_serial_drone_gets_db_metadata(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        info = {"make": "DJI", "model": "Mavic 3", "status": "accepted"}
        reg = _StubRegistry(info)
        tracker = _tracker_with_registry(publisher, clock, reg)
        await tracker.process_poll([_drone_obs("1581F5BK0001")])
        assert reg.calls == ["1581F5BK0001"]  # looked up by the serial track_id
        assert publisher.drones_published[-1].db_metadata == info

    async def test_non_serial_drone_not_looked_up(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        reg = _StubRegistry({"make": "x"})
        tracker = _tracker_with_registry(publisher, clock, reg)
        obs = AircraftObservation(
            track_id="sess-1",
            hex=None,
            non_icao=True,
            observed_at=_T0,
            seen_by="dump3411",
            band="remoteid",
            lat=30.34,
            lon=-97.98,
            drone=DroneInfo(id_type="session"),
        )
        await tracker.process_poll([obs])
        assert reg.calls == []  # session ids aren't resolvable -> never queried
        assert publisher.drones_published[-1].db_metadata == {}

    async def test_no_registry_leaves_db_metadata_empty(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        tracker = _tracker_with_registry(publisher, clock, None)
        await tracker.process_poll([_drone_obs("1581F5BK0001")])
        assert publisher.drones_published[-1].db_metadata == {}

    async def test_registry_miss_leaves_db_metadata_empty(
        self, publisher: FakePublisher, clock: Clock
    ) -> None:
        reg = _StubRegistry(None)  # serial not in the registry
        tracker = _tracker_with_registry(publisher, clock, reg)
        await tracker.process_poll([_drone_obs("unknown-serial")])
        assert reg.calls == ["unknown-serial"]
        assert publisher.drones_published[-1].db_metadata == {}
