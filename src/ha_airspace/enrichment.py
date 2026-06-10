"""Enrichment pipeline orchestrator (Phase 2a).

Runs after the tracker has updated an ``AircraftState`` and computed geometry,
before publish. The DESIGN §4 pipeline order is: DB join -> geometry -> flags
-> alerts. Geometry already lives in the tracker, so the enricher owns the
parts that depend on config rules and reference data:

* **slice 1 (now):** flag evaluation -> ``state.flags``
* **slice 2 (next):** DB join -> ``state.db_metadata`` (then DB-backed flags
  start matching)
* **slice 3:** alert ENTER/EXIT evaluation

Kept as a thin orchestrator so each slice adds a step without reshaping the
call site. The tracker holds an optional ``Enricher``; absent, behavior is
exactly Phase 1 (no flags, empty db_metadata).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ha_airspace.flags import evaluate_flags

if TYPE_CHECKING:
    from ha_airspace.config import EnrichmentConfig
    from ha_airspace.databases import DatabaseStore
    from ha_airspace.models import AircraftState


class Enricher:
    """Applies configured enrichment to an ``AircraftState`` in place.

    Construction args:
      config: The validated ``EnrichmentConfig`` (flags now; alerts later).
      db_store: Optional reference-DB store. When supplied, each state's
        ``db_metadata`` is populated from the merged Mictronics + ADSBex
        lookup *before* flags evaluate, so DB-backed flags (``sources:``)
        resolve. Absent = ``db_metadata`` stays empty (slice-1 behavior).

    # TODO(phase-2a-slice-3): run alert evaluation after flags and return the
    # ENTER/EXIT events for the tracker to publish.
    """

    def __init__(self, config: EnrichmentConfig, *, db_store: DatabaseStore | None = None) -> None:
        self._config = config
        self._db_store = db_store

    def enrich(self, state: AircraftState) -> None:
        """Enrich one state in place. Order matches DESIGN §4: DB join, then
        flags. Idempotent per poll: both ``db_metadata`` and ``flags`` are
        fully reassigned (not merged into the prior pass) so stale values
        from an earlier observation never linger."""
        # Reference DBs are keyed by ICAO hex; non-ICAO tracks (Remote ID
        # drones) have no hex to look up, so they skip DB join entirely.
        if self._db_store is not None and state.hex is not None:
            # Snapshot the current dict once (DESIGN §2): a mid-pass refresh
            # swap rebinds store.current, but our local reference is stable.
            db = self._db_store.current
            metadata = db.get(state.hex)
            state.db_metadata = dict(metadata) if metadata else {}
        state.flags = evaluate_flags(state, self._config.flags)


__all__ = ["Enricher"]
