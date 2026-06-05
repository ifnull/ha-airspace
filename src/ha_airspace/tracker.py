"""Aircraft state pipeline over a multi-source ``Merger`` (Phase 3).

Owns the per-cycle pipeline on top of the canonical state the ``Merger``
produces: per-watchpoint geometry + enrichment at ingest, then lifecycle
(NEW -> ACTIVE -> STALE -> PURGED), alert evaluation, summary, and the
active-aircraft gauge each tick. Drives the MQTT ``Publisher``.

Two entry points let the app feed multiple receivers into one merged view:

* ``ingest(obs)`` — merge one observation, recompute geometry + enrichment for
  its (possibly re-canonicalized) state. Called per observation, per receiver
  poll. The ``Merger`` does the cross-receiver canonical selection.
* ``tick()`` — run lifecycle / alerts / summary / metrics once over all merged
  states. Called on the service cadence by the app's pipeline loop.

``process_poll(observations)`` = ``ingest`` each + ``tick`` — the single-source
convenience path (and what the unit tests drive).

No network or disk of its own — every side effect is delegated to the injected
``Publisher``, so tests drive it with a fake publisher and a fixed clock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import structlog

from ha_airspace.alerts import AlertEvaluator, AlertTransition
from ha_airspace.enrichment import Enricher
from ha_airspace.geo import bearing, haversine
from ha_airspace.merger import Merger
from ha_airspace.metrics import MetricsRegistry
from ha_airspace.models import AircraftObservation, AircraftState, Lifecycle, Watchpoint
from ha_airspace.mqtt.publisher import Publisher

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
      merger: The multi-source ``Merger`` that owns canonical selection and
        the state dict. Defaults to a fresh single-window ``Merger`` (the
        single-source case is just one receiver feeding it).
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
        merger: Merger | None = None,
        enricher: Enricher | None = None,
        alerts: AlertEvaluator | None = None,
        metrics: MetricsRegistry | None = None,
        clock: Callable[[], datetime] = _default_clock,
        stale_after_s: float = 5.0,
        expire_after_s: float = 60.0,
    ) -> None:
        self._watchpoints: list[Watchpoint] = list(watchpoints)
        if not self._watchpoints:
            raise ValueError("AircraftTracker requires at least one watchpoint")
        self._publisher = publisher
        self._merger = merger if merger is not None else Merger()
        self._enricher = enricher
        self._alerts = alerts
        self._primary = self._pick_primary(self._watchpoints)
        self._metrics = metrics
        self._clock = clock
        self._stale_after_s = stale_after_s
        self._expire_after_s = expire_after_s

    @property
    def _states(self) -> dict[str, AircraftState]:
        """The merged state dict, owned by the merger."""
        return self._merger.states

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def tracked_count(self) -> int:
        """Number of aircraft currently tracked (ACTIVE or STALE)."""
        return len(self._states)

    def ingest(self, obs: AircraftObservation) -> None:
        """Merge one observation and refresh its state's geometry + enrichment.
        Called per observation; the merger handles cross-receiver canonical
        selection. Does not publish — that happens in ``tick``."""
        state = self._merger.ingest(obs)
        self._recompute_geometry(state)
        if self._enricher is not None:
            # Flags / DB join depend on the freshly merged canonical +
            # geometry, so enrich after both.
            self._enricher.enrich(state)

    async def tick(self) -> None:
        """Run lifecycle, alerts, summary, and metrics once over all merged
        states. Called on the service cadence (multi-receiver) or right after
        ``ingest`` in the single-source convenience path."""
        now = self._clock()
        purged = await self._run_lifecycle(now)
        await self._evaluate_alerts(purged)
        await self._publish_summary()
        self._update_metrics()

    async def process_poll(self, observations: list[AircraftObservation]) -> None:
        """Single-source convenience: ingest every observation, then tick.
        Equivalent to one receiver's poll feeding the pipeline."""
        for obs in observations:
            self.ingest(obs)
        await self.tick()

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

    async def _run_lifecycle(self, now: datetime) -> list[str]:
        """Classify every tracked state. PURGED -> clear retained topic and
        drop; ACTIVE/STALE -> republish (publisher throttles per-hex, and
        keeps republishing STALE so HA dashboards do not blink). Returns the
        hexes purged this cycle so alert EXITs can fire for them."""
        purged: list[str] = []
        for hex_code, state in list(self._states.items()):
            lifecycle = state.lifecycle(
                now,
                stale_after_s=self._stale_after_s,
                expire_after_s=self._expire_after_s,
            )
            if lifecycle is Lifecycle.PURGED:
                await self._publisher.purge_aircraft(hex_code)
                self._merger.remove(hex_code)
                purged.append(hex_code)
                log.debug("aircraft_purged", hex=hex_code)
                continue
            await self._publisher.publish_aircraft(state)
        return purged

    async def _evaluate_alerts(self, purged: list[str]) -> None:
        """Run the alert evaluator over the current states + this poll's
        purges, then publish each ENTER/EXIT and refresh the per-rule active
        flag. No-op when no evaluator is configured."""
        if self._alerts is None:
            return
        events = self._alerts.evaluate(self._states.values(), purged)
        touched_rules: set[str] = set()
        for event in events:
            touched_rules.add(event.rule)
            if event.transition is AlertTransition.ENTER and event.state is not None:
                await self._publisher.publish_alert(event.rule, event.state)
                log.info("alert_enter", rule=event.rule, track_id=event.track_id)
            elif event.transition is AlertTransition.EXIT:
                await self._publisher.clear_alert(event.rule, event.track_id)
                log.info("alert_exit", rule=event.rule, track_id=event.track_id)
        # Refresh the active flag for any rule that saw a transition.
        active = self._alerts.active_rules()
        for rule in touched_rules:
            await self._publisher.publish_alert_active(rule, active=rule in active)

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
