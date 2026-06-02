"""Declarative flag-rule evaluation (Phase 2a, slice 1).

Pure function: ``evaluate_flags(state, flags) -> set[str]``. No mutation of
inputs, no I/O, order-independent. The enricher assigns the result to
``state.flags`` each poll (flags can change as squawk / position / DB state
changes, so this re-runs every cycle — it is cheap set work over a handful
of rules).

Each flag config carries exactly one matcher (validated at config load):

* ``squawks``    — ``canonical.squawk`` in the list
* ``patterns``   — ``canonical.flight`` starts with any prefix
* ``types``      — ``canonical.aircraft_type`` in the list
* ``categories`` — ``canonical.category`` in the list
* ``sources``    — any referenced ``db_metadata`` field is truthy (DB-backed;
                   never matches until the Phase-2a DB loader populates
                   ``db_metadata`` — empty dict means no match, which is
                   correct, not a bug)

Matching is case-insensitive for callsign prefixes and type/category codes
(receivers vary in casing); squawks are exact string compares (they are
4-char octal codes, no casing concern).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adsb_enrich.config import FlagConfig
    from adsb_enrich.models import AircraftState


def evaluate_flags(state: AircraftState, flags: dict[str, FlagConfig]) -> set[str]:
    """Return the set of flag names that match ``state``.

    Args:
        state: The aircraft to classify. Read-only.
        flags: Flag-name -> matcher config (from ``EnrichmentConfig.flags``).

    Returns:
        The subset of flag names whose matcher is satisfied. Empty when no
        rules are configured or none match.
    """
    return {name for name, cfg in flags.items() if _matches(state, cfg)}


def _matches(state: AircraftState, cfg: FlagConfig) -> bool:
    matcher = cfg.matcher
    if matcher == "squawks":
        return _match_squawk(state, cfg)
    if matcher == "patterns":
        return _match_pattern(state, cfg)
    if matcher == "types":
        return _match_field(state.canonical.aircraft_type, cfg.types)
    if matcher == "categories":
        return _match_field(state.canonical.category, cfg.categories)
    if matcher == "sources":
        return _any_source_truthy(state, cfg.sources or [])
    raise AssertionError(f"unknown flag matcher: {matcher}")


def _match_squawk(state: AircraftState, cfg: FlagConfig) -> bool:
    squawk = state.canonical.squawk
    return squawk is not None and squawk in (cfg.squawks or [])


def _match_pattern(state: AircraftState, cfg: FlagConfig) -> bool:
    flight = state.canonical.flight
    if flight is None:
        return False
    upper = flight.upper()
    return any(upper.startswith(p.upper()) for p in (cfg.patterns or []))


def _match_field(value: str | None, wanted: list[str] | None) -> bool:
    """Case-insensitive membership for code-style fields (type, category)."""
    if value is None:
        return False
    return value.upper() in {w.upper() for w in (wanted or [])}


def _any_source_truthy(state: AircraftState, sources: list[str]) -> bool:
    """A DB-backed flag matches if any ``db:field`` reference resolves to a
    truthy value in ``state.db_metadata``. The ``db`` component is advisory
    in Phase 2a (db_metadata is one merged dict, not namespaced per source);
    we match on the ``field`` name. Pre-loader, db_metadata is ``{}`` so this
    is always False."""
    metadata = state.db_metadata
    if not metadata:
        return False
    for ref in sources:
        _db, _, field = ref.partition(":")
        if metadata.get(field):
            return True
    return False


__all__ = ["evaluate_flags"]
