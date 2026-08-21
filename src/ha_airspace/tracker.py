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
from typing import TYPE_CHECKING

import structlog

from ha_airspace.alerts import AlertEvaluator, AlertTransition
from ha_airspace.drone_registry import DroneRegistry
from ha_airspace.enrichment import Enricher
from ha_airspace.geo import bearing, closest_point_of_approach, haversine
from ha_airspace.journal import Journal
from ha_airspace.merger import Merger
from ha_airspace.metrics import MetricsRegistry
from ha_airspace.models import AircraftObservation, AircraftState, Lifecycle, Watchpoint
from ha_airspace.mqtt.payloads import FlagAircraft, FlagFeedPayload
from ha_airspace.mqtt.publisher import Publisher
from ha_airspace.orbit import OrbitDetector
from ha_airspace.photos import PhotoEnricher
from ha_airspace.spoof import SpoofDetector

if TYPE_CHECKING:
    from ha_airspace.mqtt.payloads import PhotoPayload

log = structlog.get_logger(__name__)

_KNOWN_BANDS: tuple[str, ...] = ("1090", "978")
"""Bands the active-aircraft gauge always reports, so a band emptying out
sets its gauge to 0 rather than leaving a stale value in Grafana."""

_MIN_PREDICT_SPEED_KT: float = 40.0
"""Below this ground speed the track is too slow for a meaningful closest-
approach projection (taxiing, hovering, jitter), so prediction is skipped."""

_FLAG_FEED_MAX: int = 10
"""Cap on aircraft listed in a per-flag feed. The feed's ``count`` is the true
total; the list is the nearest N so a card stays readable and the retained
payload stays small. Tune if a dense flag (e.g. a broad 'interesting') routinely
overflows it."""


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
        journal: Journal | None = None,
        photos: PhotoEnricher | None = None,
        orbit: OrbitDetector | None = None,
        spoof: SpoofDetector | None = None,
        drone_registry: DroneRegistry | None = None,
        has_drone_source: bool = False,
        feed_flags: Iterable[str] = (),
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
        self._journal = journal
        self._photos = photos
        self._orbit = orbit
        self._spoof = spoof
        self._drone_registry = drone_registry
        # Only publish the drone summary when a Remote ID source is configured,
        # so ADS-B-only installs don't get spurious empty drone topics.
        self._has_drone_source = has_drone_source
        # Flags that get a per-flag feed topic + sensor (configured flag names,
        # plus any derived flag like 'orbiting'). Empty feeds still publish so the
        # sensor reads 0 — so this is the full discovered set, not just live flags.
        self._feed_flags: tuple[str, ...] = tuple(feed_flags)
        self._primary = self._pick_primary(self._watchpoints)
        self._metrics = metrics
        self._clock = clock
        self._stale_after_s = stale_after_s
        self._expire_after_s = expire_after_s
        # Last-known flag set per track, for journaling flag transitions.
        # Pruned alongside the track on expiry so it never grows unbounded.
        self._prev_flags: dict[str, frozenset[str]] = {}
        # Drone track_ids already logged this lifetime, so the detection log
        # fires once per drone (not per poll). Dropped on purge so a genuine
        # re-appearance after the track expires logs again. SD-friendly: one
        # line per detection event, never per 1 Hz poll.
        self._logged_drones: set[str] = set()

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
        if self._journal is not None:
            # Buffer the durable first_seen / last_seen (coalesced; no disk IO
            # on this path). first_seen was already restored by the merger if
            # the track was known.
            self._journal.record(state.track_id, state.hex, state.first_seen, state.last_seen)
        self._recompute_geometry(state)
        if self._enricher is not None:
            # Flags / DB join depend on the freshly merged canonical +
            # geometry, so enrich after both.
            self._enricher.enrich(state)
        if self._orbit is not None:
            # After enrich (which reassigns flags) so the derived orbiting flag
            # survives; before transition journaling so it's recorded + alertable.
            self._orbit.update(state)
        if self._spoof is not None:
            # Same placement rationale as orbit: after enrich, before journaling,
            # so the derived spoof_suspect flag is published, alertable, recorded.
            self._spoof.update(state)
        self._record_flag_transitions(state)

    def _record_flag_transitions(self, state: AircraftState) -> None:
        """Journal flag-state changes for this track: a flag newly present is a
        ``flag_enter``, one newly absent a ``flag_exit``. No-op without a
        journal (the journal is wired once at startup, never mid-run, so the
        prev-flags bookkeeping only earns its keep when there's a sink)."""
        if self._journal is None:
            return
        prev = self._prev_flags.get(state.track_id, frozenset())
        now = state.flags
        if prev == now:
            return
        at = self._clock()
        for flag in now - prev:
            self._journal.record_event(state.track_id, "flag_enter", flag, at)
        for flag in prev - now:
            self._journal.record_event(state.track_id, "flag_exit", flag, at)
        self._prev_flags[state.track_id] = frozenset(now)

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
        """Fill ``distance_to`` / ``bearing_to`` for every watchpoint, and the
        predictive ``predicted_closest_approach_nm`` / ``predicted_eta_to_home_s``
        for the primary watchpoint, from the canonical position. Clears them all
        when the canonical observation has no position (e.g. an aircraft
        broadcasting ident but not yet a fix) so a stale value never lingers."""
        canonical = state.canonical
        if canonical.lat is None or canonical.lon is None:
            state.distance_to.clear()
            state.bearing_to.clear()
            state.predicted_closest_approach_nm = None
            state.predicted_eta_to_home_s = None
            return
        for wp in self._watchpoints:
            state.distance_to[wp.name] = haversine(wp.lat, wp.lon, canonical.lat, canonical.lon)
            # Bearing from the watchpoint toward the aircraft: "look that way".
            state.bearing_to[wp.name] = bearing(wp.lat, wp.lon, canonical.lat, canonical.lon)
        self._recompute_prediction(state)

    def _recompute_prediction(self, state: AircraftState) -> None:
        """Predicted closest approach + ETA to the primary watchpoint. Needs a
        usable velocity: track + ground speed, airborne, and above a floor speed
        (a parked/taxiing/hovering track has noisy heading and no meaningful
        projection). Sets both to ``None`` otherwise."""
        c = state.canonical
        if (
            c.lat is None
            or c.lon is None
            or c.track_deg is None
            or c.ground_speed_kt is None
            or c.ground_speed_kt < _MIN_PREDICT_SPEED_KT
            or c.on_ground
        ):
            state.predicted_closest_approach_nm = None
            state.predicted_eta_to_home_s = None
            return
        cpa_nm, eta_s = closest_point_of_approach(
            self._primary.lat, self._primary.lon, c.lat, c.lon, c.track_deg, c.ground_speed_kt
        )
        state.predicted_closest_approach_nm = cpa_nm
        state.predicted_eta_to_home_s = eta_s

    async def _run_lifecycle(self, now: datetime) -> list[str]:
        """Classify every tracked state. PURGED -> clear retained topic and
        drop; ACTIVE/STALE -> republish (publisher throttles per-hex, and
        keeps republishing STALE so HA dashboards do not blink). Returns the
        hexes purged this cycle so alert EXITs can fire for them."""
        purged: list[str] = []
        for track_id, state in list(self._states.items()):
            lifecycle = state.lifecycle(
                now,
                stale_after_s=self._stale_after_s,
                expire_after_s=self._expire_after_s,
            )
            is_drone = "remoteid" in state.bands
            if lifecycle is Lifecycle.PURGED:
                if is_drone:
                    await self._publisher.purge_drone(track_id)
                else:
                    await self._publisher.purge_aircraft(track_id)
                self._merger.remove(track_id)
                purged.append(track_id)
                # Any flags the track still carried implicitly exit on purge;
                # record them and drop the prev-flags entry so it never leaks.
                stale_flags = self._prev_flags.pop(track_id, frozenset())
                if self._journal is not None:
                    for flag in stale_flags:
                        self._journal.record_event(track_id, "flag_exit", flag, now)
                if self._orbit is not None:
                    self._orbit.forget(track_id)
                if self._spoof is not None:
                    self._spoof.forget(track_id)
                if is_drone:
                    self._logged_drones.discard(track_id)
                log.debug("track_purged", track_id=track_id)
                continue
            if is_drone:
                await self._enrich_drone(state)
                self._log_drone_detected(state)
                await self._publisher.publish_drone(state)
            else:
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
                photo = await self._photo_for(event.state)
                await self._publisher.publish_alert(event.rule, event.state, photo=photo)
                log.info("alert_enter", rule=event.rule, track_id=event.track_id)
                if self._journal is not None:
                    self._journal.record_event(
                        event.track_id, "alert_enter", event.rule, self._clock()
                    )
            elif event.transition is AlertTransition.EXIT:
                await self._publisher.clear_alert(event.rule, event.track_id)
                log.info("alert_exit", rule=event.rule, track_id=event.track_id)
                if self._journal is not None:
                    self._journal.record_event(
                        event.track_id, "alert_exit", event.rule, self._clock()
                    )
        # Refresh the active flag for any rule that saw a transition.
        active = self._alerts.active_rules()
        for rule in touched_rules:
            await self._publisher.publish_alert_active(rule, active=rule in active)

    async def _photo_for(self, state: AircraftState) -> PhotoPayload | None:
        """Aircraft photo for an alerting track, when photo enrichment is
        configured and the track has an ICAO hex (Planespotters is ICAO-keyed;
        drones have none). Fails soft inside the enricher — never raises."""
        if self._photos is None or state.hex is None:
            return None
        return await self._photos.photo_for(state.hex)

    async def _enrich_drone(self, state: AircraftState) -> None:
        """Populate ``state.db_metadata`` with FAA make/model for a serial-typed
        drone, when the registry is configured. Only ``id_type == "serial"`` is
        resolvable (the broadcast serial is the track_id); session/uuid/caa_reg
        ids have nothing to look up. Cached + fails-soft in the registry."""
        if self._drone_registry is None:
            return
        drone = state.canonical.drone
        if drone is None or drone.id_type != "serial":
            return
        info = await self._drone_registry.lookup(state.track_id)
        if info is not None:
            state.db_metadata = info

    def _log_drone_detected(self, state: AircraftState) -> None:
        """Emit one structured ``drone_detected`` line the first time a drone
        track is seen (after enrichment, so make/model is resolved). This is the
        durable detection record — it survives in journald / ``docker logs``
        independent of MQTT retention, so past detections can be audited even
        after the retained topics rotate. Re-fires only after the track is
        purged (a genuine new appearance), not on every poll."""
        if state.track_id in self._logged_drones:
            return
        self._logged_drones.add(state.track_id)
        drone = state.canonical.drone
        log.info(
            "drone_detected",
            track_id=state.track_id,
            id_type=drone.id_type if drone else None,
            make=state.db_metadata.get("make"),
            model=state.db_metadata.get("model"),
            self_id=drone.self_id if drone else None,
            distance_nm=state.distance_to.get(self._primary.name),
            agl_ft=drone.agl_ft if drone else None,
            operator_located=bool(drone and drone.operator_lat is not None),
            operator_location_type=drone.operator_location_type if drone else None,
        )

    async def _publish_summary(self) -> None:
        # Aircraft and drones are counted + "nearest"-ranked separately: the
        # aircraft summary excludes drones, and drones get their own summary.
        aircraft = [s for s in self._states.values() if "remoteid" not in s.bands]
        drones = [s for s in self._states.values() if "remoteid" in s.bands]
        nearest_aircraft = self._nearest(aircraft)
        # Photo for the nearest aircraft only: a single, throttled entity, so the
        # cached/fails-soft lookup is bounded (unlike the per-hex wildcard, which
        # never carries a photo). No-op when photos are disabled or it has no hex.
        nearest_photo = (
            await self._photo_for(nearest_aircraft) if nearest_aircraft is not None else None
        )
        await self._publisher.publish_summary(
            count=len(aircraft),
            nearest=nearest_aircraft,
            count_by_flag=self._count_by_flag(aircraft),
            flag_feeds=await self._build_flag_feeds(aircraft),
            nearest_photo=nearest_photo,
        )
        if self._has_drone_source:
            await self._publisher.publish_drone_summary(
                count=len(drones),
                nearest=self._nearest(drones),
            )

    def _count_by_flag(self, states: list[AircraftState]) -> dict[str, int]:
        """How many of ``states`` carry each flag. A flag is counted once per
        track; a track with multiple flags adds to each. Flags no track
        currently carries are omitted (absent means zero)."""
        counts: dict[str, int] = {}
        for state in states:
            for flag in state.flags:
                counts[flag] = counts.get(flag, 0) + 1
        return counts

    async def _build_flag_feeds(self, states: list[AircraftState]) -> dict[str, FlagFeedPayload]:
        """One ``FlagFeedPayload`` per configured feed flag: the aircraft carrying
        that flag, nearest-to-primary first, capped at ``_FLAG_FEED_MAX``. Every
        feed flag is emitted even when nothing matches (count 0, empty list) so
        the discovered sensor reads 0 instead of going unavailable. Returns ``{}``
        when no feed flags are configured (no by_flag topics published).

        Each feed also carries ``photo`` — the Planespotters photo of its *nearest
        matching* aircraft only (one cached/fails-soft lookup per non-empty flag),
        so a flag card can spotlight the closest match without a per-row lookup."""
        if not self._feed_flags:
            return {}
        key = self._primary.name
        feeds: dict[str, FlagFeedPayload] = {}
        for flag in self._feed_flags:
            matching = [s for s in states if flag in s.flags]
            # Nearest first; unpositioned tracks (no distance to primary) sort last.
            matching.sort(key=lambda s: s.distance_to.get(key, float("inf")))
            rows = [FlagAircraft.from_state(s, watchpoint=key) for s in matching[:_FLAG_FEED_MAX]]
            nearest = matching[0] if matching else None
            photo = await self._photo_for(nearest) if nearest is not None else None
            feeds[flag] = FlagFeedPayload(
                flag=flag,
                count=len(matching),
                watchpoint=key,
                aircraft=rows,
                # Nearest match's position, so the flag sensor maps as one marker.
                latitude=nearest.canonical.lat if nearest is not None else None,
                longitude=nearest.canonical.lon if nearest is not None else None,
                photo=photo,
            )
        return feeds

    def _nearest(self, states: list[AircraftState]) -> AircraftState | None:
        """The track in ``states`` closest to the primary watchpoint. Tracks
        without a position (no distance to the primary) are skipped. Returns
        None when nothing positioned is present — the publisher then clears the
        nearest topic so HA goes unavailable rather than stale."""
        key = self._primary.name
        best: AircraftState | None = None
        best_distance: float | None = None
        for state in states:
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
