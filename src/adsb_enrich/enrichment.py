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

from adsb_enrich.flags import evaluate_flags

if TYPE_CHECKING:
    from adsb_enrich.config import EnrichmentConfig
    from adsb_enrich.models import AircraftState


class Enricher:
    """Applies configured enrichment to an ``AircraftState`` in place.

    Construction args:
      config: The validated ``EnrichmentConfig`` (flags now; alerts later).

    # TODO(phase-2a-slice-2): take the DB store; run db-join before flags so
    # DB-backed flags resolve.
    # TODO(phase-2a-slice-3): run alert evaluation after flags and return the
    # ENTER/EXIT events for the tracker to publish.
    """

    def __init__(self, config: EnrichmentConfig) -> None:
        self._config = config

    def enrich(self, state: AircraftState) -> None:
        """Enrich one state in place. Idempotent per poll: ``flags`` is fully
        recomputed (assigned, not unioned) so a flag that stops matching —
        squawk cleared, aircraft moved — is correctly removed."""
        state.flags = evaluate_flags(state, self._config.flags)


__all__ = ["Enricher"]
