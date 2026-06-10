"""Multi-source merge: N receivers -> one canonical AircraftState per hex.

The Phase-3 core that replaces the single-source tracker's latest-wins ingest.
Pure and clock-free: it consumes observations and maintains the canonical state
dict; the tracker/pipeline still owns lifecycle, geometry, enrichment, alerts,
and publishing (wired in slice 2).

Two pieces:

* ``select_canonical`` — pure: given several observations of the *same* hex,
  pick the authoritative one by the locked DESIGN §3 order:

    1. fresh position (``seen_pos_age_s`` < 5 s) preferred over stale/no position
    2. higher ``nic`` (Navigation Integrity Category)
    3. higher ``nac_p`` (Navigation Accuracy Category, position)
    4. freshest ``seen_pos_age_s``
    5. higher ``rssi_dbfs``
    6. alphabetical receiver name (deterministic tiebreak)

* ``Merger`` — owns ``states`` keyed by hex; ``by_receiver`` holds each
  receiver's latest observation. On ingest it recomputes canonical from the
  observations that are *current* — within ``canonical_window_s`` of the
  freshest observation for that hex — so a receiver that has fallen behind
  cannot keep winning with a frozen-fresh snapshot. ``seen_by`` and ``bands``
  accumulate; ``last_seen`` is the newest ``observed_at`` across receivers.

States are keyed by ``track_id`` (ICAO hex for ADS-B, UAS id for Remote ID),
so drone and ICAO namespaces coexist without cross-matching.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ha_airspace.models import AircraftState

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

    from ha_airspace.models import AircraftObservation

FRESH_POSITION_S: float = 5.0
"""A position is "fresh" if the receiver saw it within this many seconds
(``seen_pos_age_s``). Matches DESIGN §3 step 1."""

DEFAULT_CANONICAL_WINDOW_S: float = 5.0
"""When recomputing canonical, only consider per-receiver observations whose
``observed_at`` is within this window of the freshest observation for the hex.
Excludes a lagging receiver's frozen snapshot from the selection (it stays in
``by_receiver`` for diagnostics). Relative to the freshest obs, so no external
clock is needed."""


def select_canonical(observations: Iterable[AircraftObservation]) -> AircraftObservation:
    """Pick the authoritative observation by the locked DESIGN §3 order.

    Raises ``ValueError`` on an empty input — callers always have at least one.
    Deterministic: the final tiebreak is the receiver name, so the same set of
    observations always yields the same canonical.
    """
    candidates = list(observations)
    if not candidates:
        raise ValueError("select_canonical requires at least one observation")
    return min(candidates, key=_canonical_sort_key)


def _canonical_sort_key(
    obs: AircraftObservation,
) -> tuple[int, float, float, float, float, str]:
    """Lower is better, so ``min`` picks the best. Each component encodes one
    DESIGN §3 rung; ``None`` quality fields sort worst (``inf``)."""
    seen_pos = obs.seen_pos_age_s
    fresh = seen_pos is not None and seen_pos < FRESH_POSITION_S
    return (
        0 if fresh else 1,  # fresh position first
        -obs.nic if obs.nic is not None else math.inf,  # higher NIC first
        -obs.nac_p if obs.nac_p is not None else math.inf,  # higher NAC_p first
        seen_pos if seen_pos is not None else math.inf,  # freshest position
        -obs.rssi_dbfs if obs.rssi_dbfs is not None else math.inf,  # stronger RSSI
        obs.seen_by,  # alphabetical receiver name
    )


class Merger:
    """Maintains canonical ``AircraftState`` across multiple receivers.

    Construction args:
      canonical_window_s: Observations older than this (relative to the
        freshest observation for a hex) are excluded from canonical selection.
        Defaults to ``DEFAULT_CANONICAL_WINDOW_S``.
      first_seen_for: Optional ``track_id -> datetime | None`` lookup. On new-
        track creation, a hit overrides the fresh ``observed_at`` so a track
        reappearing after a restart keeps its original ``first_seen`` (Phase 2b
        durable history). Injected as a plain callable so the merger stays
        IO-free — the journal is not imported here.

    Not async, no IO. The tracker drives ``ingest`` per observation and reads
    ``states`` to run the rest of the pipeline.
    """

    def __init__(
        self,
        *,
        canonical_window_s: float = DEFAULT_CANONICAL_WINDOW_S,
        first_seen_for: Callable[[str], datetime | None] | None = None,
    ) -> None:
        self._window_s = canonical_window_s
        self._first_seen_for = first_seen_for
        self.states: dict[str, AircraftState] = {}

    def ingest(self, obs: AircraftObservation) -> AircraftState:
        """Upsert one observation; return the (re)merged state for its hex.

        New hex -> fresh state (with ``first_seen`` restored from the journal
        lookup if one exists). Existing hex -> record this receiver's latest
        observation, accumulate ``seen_by`` / ``bands``, advance ``last_seen``
        to the newest ``observed_at``, and recompute canonical among the
        current observations.
        """
        state = self.states.get(obs.track_id)
        if state is None:
            state = AircraftState.from_first_observation(obs)
            if self._first_seen_for is not None:
                prior = self._first_seen_for(obs.track_id)
                if prior is not None:
                    state.first_seen = prior
            self.states[obs.track_id] = state
            return state

        state.by_receiver[obs.seen_by] = obs
        state.seen_by.add(obs.seen_by)
        state.bands.add(obs.band)
        state.last_seen = max(state.last_seen, obs.observed_at)

        canonical = self._select_current(state)
        state.canonical = canonical
        state.canonical_source = canonical.seen_by
        return state

    def remove(self, track_id: str) -> None:
        """Drop a track by id (e.g. after the lifecycle purges it). Idempotent."""
        self.states.pop(track_id, None)

    def _select_current(self, state: AircraftState) -> AircraftObservation:
        """Canonical among observations within the window of the freshest one,
        so a lagging receiver's frozen snapshot does not keep winning."""
        observations = list(state.by_receiver.values())
        freshest = max(o.observed_at for o in observations)
        current = [
            o for o in observations if (freshest - o.observed_at).total_seconds() <= self._window_s
        ]
        # current is never empty — the freshest observation is always within
        # its own zero-second window.
        return select_canonical(current)


__all__ = ["DEFAULT_CANONICAL_WINDOW_S", "FRESH_POSITION_S", "Merger", "select_canonical"]
