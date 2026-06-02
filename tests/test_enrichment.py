"""Tests for the Enricher orchestrator (Phase 2a slice 1: flags only)."""

from __future__ import annotations

from datetime import UTC, datetime

from adsb_enrich.config import EnrichmentConfig, FlagConfig
from adsb_enrich.enrichment import Enricher
from adsb_enrich.models import AircraftObservation, AircraftState

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _state(*, squawk: str | None = None, flight: str | None = None) -> AircraftState:
    obs = AircraftObservation(
        hex="ae0001",
        observed_at=_T0,
        seen_by="rx",
        band="1090",
        flight=flight,
        squawk=squawk,
    )
    return AircraftState.from_first_observation(obs)


def test_enrich_sets_matching_flags() -> None:
    enricher = Enricher(EnrichmentConfig(flags={"emergency": FlagConfig(squawks=["7700"])}))
    state = _state(squawk="7700")
    enricher.enrich(state)
    assert state.flags == {"emergency"}


def test_enrich_recomputes_flags_each_call() -> None:
    # A flag that stops matching must be removed, not left stale.
    enricher = Enricher(EnrichmentConfig(flags={"emergency": FlagConfig(squawks=["7700"])}))
    state = _state(squawk="7700")
    enricher.enrich(state)
    assert state.flags == {"emergency"}

    # Squawk cleared on the next observation — flag should drop.
    state.canonical = AircraftObservation(
        hex="ae0001", observed_at=_T0, seen_by="rx", band="1090", squawk=None
    )
    enricher.enrich(state)
    assert state.flags == set()


def test_empty_enrichment_clears_flags() -> None:
    enricher = Enricher(EnrichmentConfig())
    state = _state(squawk="7700")
    state.flags = {"stale"}  # pretend a previous pass set something
    enricher.enrich(state)
    assert state.flags == set()
