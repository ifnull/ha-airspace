"""Alert rule evaluation with ENTER/EXIT transitions (Phase 2a, slice 3).

Two layers:

* ``rule_matches`` — pure: does this ``AircraftState`` satisfy a rule's match
  block *right now*? AND across keys, OR within a list (DESIGN §4, locked).
* ``AlertEvaluator`` — stateful: tracks per-``(rule, hex)`` membership across
  polls and emits ``AlertEvent`` transitions. ENTER when an aircraft starts
  matching; EXIT when it stops (or is purged). After EXIT a per-rule cooldown
  blocks re-ENTER for the same hex, so a condition oscillating near a
  threshold does not thrash the alert. Time is injected (CLAUDE.md) so cooldown
  is deterministic in tests.

The evaluator is the poll-spanning, stateful counterpart to the per-state
``Enricher`` (which is stateless). The tracker drives it once per poll with the
current set of states and the set of hexes that were purged, and publishes the
returned events.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ha_airspace.config import AlertsConfig, MatchBlock
    from ha_airspace.models import AircraftState


class AlertTransition(StrEnum):
    ENTER = "enter"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """One ENTER/EXIT transition the tracker should publish.

    ``state`` is the matching track on ENTER; ``None`` on EXIT (the track may
    have been purged, so we only carry the ``track_id``)."""

    rule: str
    track_id: str
    transition: AlertTransition
    state: AircraftState | None


def _default_clock() -> datetime:
    return datetime.now(UTC)


def rule_matches(
    state: AircraftState,
    match: MatchBlock,
    *,
    elevation_m_for: Callable[[str], float | None],
) -> bool:
    """True if ``state`` satisfies every condition in ``match`` (AND); within
    a single list-valued condition any item suffices (OR).

    ``elevation_m_for`` resolves a watchpoint name to its elevation for the
    v1 AGL approximation; only called when ``max_alt_agl_ft`` is set.
    """
    if match.flags is not None and not (state.flags & set(match.flags)):
        return False

    if match.category is not None:
        category = state.canonical.category
        if category is None or category.upper() not in {c.upper() for c in match.category}:
            return False

    wp_name = match.watchpoint or "home"

    if match.max_distance_nm is not None:
        distance = state.distance_to.get(wp_name)
        if distance is None or distance > match.max_distance_nm:
            return False

    if match.max_alt_agl_ft is not None:
        alt_msl = state.canonical.alt_baro_ft
        if alt_msl is None:
            return False
        elevation_m = elevation_m_for(wp_name)
        # v1 AGL: MSL minus watchpoint ground elevation (config-validated to
        # exist when this condition is used). 1 m = 3.28084 ft.
        ground_ft = (elevation_m or 0.0) * 3.28084
        if (alt_msl - ground_ft) > match.max_alt_agl_ft:
            return False

    return True


class AlertEvaluator:
    """Stateful ENTER/EXIT alert evaluation across polls.

    Construction args:
      config: The ``AlertsConfig`` (rules + cooldown).
      elevation_m_for: Watchpoint-name -> elevation (m) resolver, for AGL.
      clock: Current UTC time; injected for deterministic cooldown tests.
    """

    def __init__(
        self,
        config: AlertsConfig,
        *,
        elevation_m_for: Callable[[str], float | None],
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._config = config
        self._elevation_m_for = elevation_m_for
        self._clock = clock
        # (rule, hex) currently inside the rule.
        self._active: set[tuple[str, str]] = set()
        # (rule, hex) -> time of last EXIT, for cooldown gating.
        self._last_exit: dict[tuple[str, str], datetime] = {}

    def evaluate(
        self, states: Iterable[AircraftState], purged_ids: Iterable[str]
    ) -> list[AlertEvent]:
        """Process one poll. ``states`` are the currently-tracked tracks;
        ``purged_ids`` are track_ids dropped this cycle (force EXIT). Returns
        the ENTER/EXIT events, in rule-then-id order for determinism."""
        now = self._clock()
        by_id = {s.track_id: s for s in states}
        events: list[AlertEvent] = []

        for rule in self._config.rules:
            currently = {
                track_id
                for track_id, state in by_id.items()
                if rule_matches(state, rule.match, elevation_m_for=self._elevation_m_for)
            }
            events.extend(self._transitions(rule.name, currently, by_id, now))

        # Any active (rule, id) whose track was purged exits regardless of rule.
        events.extend(self._handle_purges(set(purged_ids), now))
        return events

    def _transitions(
        self,
        rule_name: str,
        currently: set[str],
        by_id: dict[str, AircraftState],
        now: datetime,
    ) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for track_id in sorted(currently):
            key = (rule_name, track_id)
            if key in self._active:
                continue  # already inside; no transition
            if self._in_cooldown(key, now):
                continue  # matched again too soon after EXIT; suppress
            self._active.add(key)
            events.append(
                AlertEvent(rule_name, track_id, AlertTransition.ENTER, by_id.get(track_id))
            )

        # EXITs: active for this rule but no longer matching.
        for rule, track_id in sorted(self._active):
            if rule != rule_name or track_id in currently:
                continue
            self._exit((rule, track_id), now, events)
        return events

    def _handle_purges(self, purged: set[str], now: datetime) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for rule, track_id in sorted(self._active):
            if track_id in purged:
                self._exit((rule, track_id), now, events)
        return events

    def _exit(self, key: tuple[str, str], now: datetime, events: list[AlertEvent]) -> None:
        self._active.discard(key)
        self._last_exit[key] = now
        events.append(AlertEvent(key[0], key[1], AlertTransition.EXIT, None))

    def _in_cooldown(self, key: tuple[str, str], now: datetime) -> bool:
        last = self._last_exit.get(key)
        if last is None:
            return False
        return (now - last).total_seconds() < self._config.cooldown_s

    def active_rules(self) -> set[str]:
        """Rule names with at least one aircraft currently inside — drives the
        per-rule ``binary_sensor`` active topic."""
        return {rule for rule, _hex in self._active}


__all__ = ["AlertEvaluator", "AlertEvent", "AlertTransition", "rule_matches"]
