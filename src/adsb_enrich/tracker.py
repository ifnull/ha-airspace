"""Single-source aircraft state tracking (Phase 1).

Maintains the canonical ``AircraftState`` dict for one receiver, runs the
NEW -> ACTIVE -> STALE -> PURGED lifecycle each poll, computes per-watchpoint
geometry, picks the nearest aircraft, and drives the MQTT ``Publisher``.

This is the Phase 1 degenerate case of the multi-source merger: a single
receiver, so "canonical" is always the latest observation and there is no
cross-receiver position selection to do. The lifecycle / geometry / nearest /
publish logic established here is what Phase 3 keeps.

# TODO(phase-3): canonical selection (NIC -> NAC_p -> seen_pos -> RSSI -> name)
# moves to merger.py; the tracker then consumes already-merged states instead
# of doing the trivial latest-wins update in _ingest().

No network or disk of its own — every side effect is delegated to the injected
``Publisher``, so tests drive it with a fake publisher and a fixed clock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import structlog

from adsb_enrich.enrichment import Enricher
from adsb_enrich.geo import bearing, haversine
from adsb_enrich.metrics import MetricsRegistry
from adsb_enrich.models import AircraftObservation, AircraftState, Lifecycle, Watchpoint
from adsb_enrich.mqtt.publisher import Publisher

log = structlog.get_logger(__name__)

_KNOWN_BANDS: tuple[str, ...] = ("1090", "978")
"""Bands the active-aircraft gauge always reports, so a band emptying out
sets its gauge to 0 rather than leaving a stale value in Grafana."""


def _default_clock() -> datetime:
    return datetime.now(UTC)


class AircraftTracker:
    """Maintains single-receiver aircraft state and publishes it.

    Call ``process_poll(observations)`` once per receiver poll. Each call:

      1. Ingests the poll's observations (upsert into the state dict;
         latest observation wins as canonical for the single-receiver case).
      2. Recomputes per-watchpoint distance/bearing for updated states.
      3. Runs the lifecycle over *all* tracked states (not just this poll's):
         PURGED states clear their retained topic and drop out; ACTIVE and
         STALE states republish (the publisher throttles per-hex).
      4. Publishes the summary (count + nearest-to-primary-watchpoint).
      5. Updates the active-aircraft gauge.

    Construction args:
      publisher: The MQTT ``Publisher``. The tracker calls
        ``publish_aircraft`` / ``purge_aircraft`` / ``publish_summary``.
      watchpoints: Runtime watchpoints. Distance/bearing are computed for
        every one; "nearest" is measured to the primary (the one named
        ``home`` if present, else the first).
      enricher: Optional ``Enricher``. When supplied, each updated state is
        enriched (flags now; DB join + alerts later) after geometry and
        before publish. Absent = Phase 1 behavior (no flags).
      metrics: Optional ``MetricsRegistry`` for the active-aircraft gauge.
      clock: Returns the current UTC ``datetime``. Injected for
        deterministic lifecycle tests (CLAUDE.md "time is a fixture").
      stale_after_s / expire_after_s: Lifecycle thresholds passed through
        to ``AircraftState.lifecycle``. Defaults match the model's. Not
        config-driven yet.
        # TODO(phase-1): surface these as service-config knobs once the
        # field set has settled.
    """

    def __init__(
        self,
        publisher: Publisher,
        watchpoints: Iterable[Watchpoint],
        *,
        enricher: Enricher | None = None,
        metrics: MetricsRegistry | None = None,
        clock: Callable[[], datetime] = _default_clock,
        stale_after_s: float = 5.0,
        expire_after_s: float = 60.0,
    ) -> None:
        self._watchpoints: list[Watchpoint] = list(watchpoints)
        if not self._watchpoints:
            raise ValueError("AircraftTracker requires at least one watchpoint")
        self._publisher = publisher
        self._enricher = enricher
        self._primary = self._pick_primary(self._watchpoints)
        self._metrics = metrics
        self._clock = clock
        self._stale_after_s = stale_after_s
        self._expire_after_s = expire_after_s
        self._states: dict[str, AircraftState] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def tracked_count(self) -> int:
        """Number of aircraft currently tracked (ACTIVE or STALE)."""
        return len(self._states)

    async def process_poll(self, observations: list[AircraftObservation]) -> None:
        """Process one receiver poll. See class docstring for the sequence."""
        now = self._clock()
        for obs in observations:
            self._ingest(obs)
        await self._run_lifecycle(now)
        await self._publish_summary()
        self._update_metrics()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_primary(watchpoints: list[Watchpoint]) -> Watchpoint:
        """Primary watchpoint for the "nearest" summary: the one named
        ``home`` if present, else the first. Mirrors the config default
        (rules omit ``watchpoint`` iff there is one watchpoint named home)."""
        for wp in watchpoints:
            if wp.name == "home":
                return wp
        return watchpoints[0]

    def _ingest(self, obs: AircraftObservation) -> None:
        """Upsert one observation. New hex -> fresh state; existing hex ->
        latest-wins update (single-receiver canonical policy)."""
        state = self._states.get(obs.hex)
        if state is None:
            state = AircraftState.from_first_observation(obs)
            self._states[obs.hex] = state
        else:
            state.canonical = obs
            state.canonical_source = obs.seen_by
            state.last_seen = obs.observed_at
            state.bands.add(obs.band)
            state.seen_by.add(obs.seen_by)
            state.by_receiver[obs.seen_by] = obs
        self._recompute_geometry(state)
        if self._enricher is not None:
            # Flags (and, later, DB join + alerts) depend on the freshly
            # updated canonical + geometry, so enrich after both.
            self._enricher.enrich(state)

    def _recompute_geometry(self, state: AircraftState) -> None:
        """Fill ``distance_to`` / ``bearing_to`` for every watchpoint from
        the canonical position. Clears them when the canonical observation
        has no position (e.g. an aircraft broadcasting ident but not yet a
        fix) so a stale distance never lingers."""
        canonical = state.canonical
        if canonical.lat is None or canonical.lon is None:
            state.distance_to.clear()
            state.bearing_to.clear()
            return
        for wp in self._watchpoints:
            state.distance_to[wp.name] = haversine(wp.lat, wp.lon, canonical.lat, canonical.lon)
            # Bearing from the watchpoint toward the aircraft: "look that way".
            state.bearing_to[wp.name] = bearing(wp.lat, wp.lon, canonical.lat, canonical.lon)

    async def _run_lifecycle(self, now: datetime) -> None:
        """Classify every tracked state. PURGED -> clear retained topic and
        drop; ACTIVE/STALE -> republish (publisher throttles per-hex, and
        keeps republishing STALE so HA dashboards do not blink)."""
        for hex_code, state in list(self._states.items()):
            lifecycle = state.lifecycle(
                now,
                stale_after_s=self._stale_after_s,
                expire_after_s=self._expire_after_s,
            )
            if lifecycle is Lifecycle.PURGED:
                await self._publisher.purge_aircraft(hex_code)
                del self._states[hex_code]
                log.debug("aircraft_purged", hex=hex_code)
                continue
            await self._publisher.publish_aircraft(state)

    async def _publish_summary(self) -> None:
        nearest = self._nearest()
        await self._publisher.publish_summary(
            count=len(self._states),
            nearest=nearest,
            count_by_flag=self._count_by_flag(),
        )

    def _count_by_flag(self) -> dict[str, int]:
        """How many tracked aircraft carry each flag. A flag is counted once
        per aircraft; an aircraft with multiple flags adds to each. Flags that
        no aircraft currently carry are omitted (the topic reflects the live
        airspace; absent means zero)."""
        counts: dict[str, int] = {}
        for state in self._states.values():
            for flag in state.flags:
                counts[flag] = counts.get(flag, 0) + 1
        return counts

    def _nearest(self) -> AircraftState | None:
        """The tracked aircraft closest to the primary watchpoint. Aircraft
        without a position (no distance to the primary) are skipped. Returns
        None when nothing positioned is in coverage — the publisher then
        clears the nearest topic so HA goes unavailable rather than stale."""
        key = self._primary.name
        best: AircraftState | None = None
        best_distance: float | None = None
        for state in self._states.values():
            distance = state.distance_to.get(key)
            if distance is None:
                continue
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best = state
        return best

    def _update_metrics(self) -> None:
        if self._metrics is None:
            return
        counts: dict[str, int] = dict.fromkeys(_KNOWN_BANDS, 0)
        for state in self._states.values():
            for band in state.bands:
                counts[band] = counts.get(band, 0) + 1
        for band, count in counts.items():
            self._metrics.aircraft_active.labels(band=band).set(count)


__all__ = ["AircraftTracker"]
