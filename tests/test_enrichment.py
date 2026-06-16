"""Tests for the Enricher orchestrator (Phase 2a slice 1: flags only)."""

from __future__ import annotations

from datetime import UTC, datetime

from ha_airspace.config import EnrichmentConfig, FlagConfig
from ha_airspace.databases import DatabaseStore
from ha_airspace.enrichment import Enricher
from ha_airspace.models import AircraftObservation, AircraftState

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


# ---------------------------------------------------------------------------
# DB join (slice 2)
# ---------------------------------------------------------------------------


def _mil_state(hex_code: str = "ae292b") -> AircraftState:
    obs = AircraftObservation(hex=hex_code, observed_at=_T0, seen_by="rx", band="1090")
    return AircraftState.from_first_observation(obs)


def test_db_join_populates_metadata_and_resolves_flag() -> None:
    store = DatabaseStore()
    store.swap({"ae292b": {"mil": True, "model": "E-6B Mercury"}})
    enricher = Enricher(
        EnrichmentConfig(flags={"military": FlagConfig(sources=["adsbexchange:mil"])}),
        db_store=store,
    )
    state = _mil_state("ae292b")
    enricher.enrich(state)
    # db_metadata populated from the store, and the DB-backed flag now matches.
    assert state.db_metadata == {"mil": True, "model": "E-6B Mercury"}
    assert state.flags == {"military"}


def test_db_type_backfills_aircraft_type() -> None:
    # Broadcast omits `t` (military) — the DB type fills aircraft_type.
    store = DatabaseStore()
    store.swap({"ae292b": {"type": "E6", "model": "E-6B Mercury"}})
    enricher = Enricher(EnrichmentConfig(), db_store=store)
    state = _mil_state("ae292b")
    assert state.canonical.aircraft_type is None
    enricher.enrich(state)
    assert state.canonical.aircraft_type == "E6"


def test_broadcast_type_not_overwritten_by_db() -> None:
    store = DatabaseStore()
    store.swap({"ae292b": {"type": "E6"}})
    enricher = Enricher(EnrichmentConfig(), db_store=store)
    obs = AircraftObservation(
        hex="ae292b", observed_at=_T0, seen_by="rx", band="1090", aircraft_type="B738"
    )
    state = AircraftState.from_first_observation(obs)
    enricher.enrich(state)
    assert state.canonical.aircraft_type == "B738"  # broadcast wins


def test_types_flag_matches_via_backfilled_db_type() -> None:
    store = DatabaseStore()
    store.swap({"ae292b": {"type": "C17"}})
    enricher = Enricher(
        EnrichmentConfig(flags={"heavy_mil": FlagConfig(types=["C17", "B52"])}),
        db_store=store,
    )
    state = _mil_state("ae292b")
    enricher.enrich(state)
    assert state.flags == {"heavy_mil"}  # types matcher saw the backfilled type


def test_db_join_empty_for_unknown_hex() -> None:
    store = DatabaseStore()
    store.swap({"ae292b": {"mil": True}})
    enricher = Enricher(
        EnrichmentConfig(flags={"military": FlagConfig(sources=["adsbexchange:mil"])}),
        db_store=store,
    )
    state = _mil_state("abcdef")  # not in the DB
    enricher.enrich(state)
    assert state.db_metadata == {}
    assert state.flags == set()


def test_db_metadata_reassigned_each_pass() -> None:
    # A hex that leaves the DB (or a store swap) must clear stale metadata.
    store = DatabaseStore()
    store.swap({"ae292b": {"mil": True}})
    enricher = Enricher(EnrichmentConfig(), db_store=store)
    state = _mil_state("ae292b")
    enricher.enrich(state)
    assert state.db_metadata == {"mil": True}
    # Store swapped to one without this hex -> next pass clears metadata.
    store.swap({})
    enricher.enrich(state)
    assert state.db_metadata == {}
