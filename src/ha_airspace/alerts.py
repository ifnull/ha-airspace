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

    ``state`` is the matching aircraft on ENTER; ``None`` on EXIT (the
    aircraft may have been purged, so we only carry the hex)."""

    rule: str
    hex: str
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
        self, states: Iterable[AircraftState], purged_hexes: Iterable[str]
    ) -> list[AlertEvent]:
        """Process one poll. ``states`` are the currently-tracked aircraft;
        ``purged_hexes`` are those dropped this cycle (force EXIT). Returns the
        ENTER/EXIT events to publish, in rule-then-hex order for determinism."""
        now = self._clock()
        by_hex = {s.hex: s for s in states}
        events: list[AlertEvent] = []

        for rule in self._config.rules:
            currently = {
                hex_code
                for hex_code, state in by_hex.items()
                if rule_matches(state, rule.match, elevation_m_for=self._elevation_m_for)
            }
            events.extend(self._transitions(rule.name, currently, by_hex, now))

        # Any active (rule, hex) whose hex was purged exits regardless of rule.
        events.extend(self._handle_purges(set(purged_hexes), now))
        return events

    def _transitions(
        self,
        rule_name: str,
        currently: set[str],
        by_hex: dict[str, AircraftState],
        now: datetime,
    ) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for hex_code in sorted(currently):
            key = (rule_name, hex_code)
            if key in self._active:
                continue  # already inside; no transition
            if self._in_cooldown(key, now):
                continue  # matched again too soon after EXIT; suppress
            self._active.add(key)
            events.append(
                AlertEvent(rule_name, hex_code, AlertTransition.ENTER, by_hex.get(hex_code))
            )

        # EXITs: active for this rule but no longer matching.
        for rule, hex_code in sorted(self._active):
            if rule != rule_name or hex_code in currently:
                continue
            self._exit((rule, hex_code), now, events)
        return events

    def _handle_purges(self, purged: set[str], now: datetime) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for rule, hex_code in sorted(self._active):
            if hex_code in purged:
                self._exit((rule, hex_code), now, events)
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
