"""Tests for the reference-DB parsers, store, and loader.

Parsers run against small gzip'd fixtures derived from the real Mictronics
and ADSBexchange files (captured 2026-06-02) — no network. The loader is
driven with a fake fetcher so the download path is exercised without HTTP;
the real network download lives in tests/integration.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from ha_airspace.config import DatabasesConfig, DatabaseSourceConfig
from ha_airspace.databases import (
    DatabaseLoader,
    DatabaseStore,
    parse_adsbexchange,
    parse_mictronics,
)

_DB_FIXTURES = Path(__file__).parent / "fixtures" / "db"
_MIC = (_DB_FIXTURES / "mictronics_sample.csv.gz").read_bytes()
_ADSBEX = (_DB_FIXTURES / "adsbexchange_sample.jsonl.gz").read_bytes()


# ---------------------------------------------------------------------------
# Mictronics parser
# ---------------------------------------------------------------------------


class TestMictronicsParser:
    def test_parses_rows_keyed_by_lowercase_hex(self) -> None:
        db = parse_mictronics(_MIC)
        assert "ae292b" in db  # was uppercase-ish in source, normalized
        assert "004002" in db

    def test_military_flag_from_dbflags(self) -> None:
        db = parse_mictronics(_MIC)
        # dbFlags is LSB-first: "10" = mil only; "00" = none.
        assert db["ae292b"].get("mil") is True
        assert "mil" not in db["004002"]

    def test_multiple_dbflags_decoded(self) -> None:
        db = parse_mictronics(_MIC)
        # ae49e0 (EA-18G Growler) has "11000" = mil + interesting.
        assert db["ae49e0"].get("mil") is True
        assert db["ae49e0"].get("interesting") is True
        # abc123 has "0001" = LADD only.
        assert db["abc123"].get("ladd") is True
        assert "mil" not in db["abc123"]

    def test_reg_and_type_mapped(self) -> None:
        db = parse_mictronics(_MIC)
        assert db["004002"]["reg"] == "Z-WPA"
        assert db["004002"]["type"] == "B732"

    def test_sparse_empty_fields_omitted(self) -> None:
        db = parse_mictronics(_MIC)
        # abc123 has reg N12345 but empty type — type key absent, not "".
        assert db["abc123"]["reg"] == "N12345"
        assert "type" not in db["abc123"]

    def test_malformed_rows_skipped(self) -> None:
        raw = gzip.compress(b"tooFewFields\nae0001;N1;C172;00;Cessna;;;\n")
        db = parse_mictronics(raw)
        assert "ae0001" in db
        assert len(db) == 1


# ---------------------------------------------------------------------------
# ADSBexchange parser
# ---------------------------------------------------------------------------


class TestAdsbexchangeParser:
    def test_parses_ndjson_keyed_by_lowercase_hex(self) -> None:
        db = parse_adsbexchange(_ADSBEX)
        assert "ae292b" in db
        assert "ac738e" in db

    def test_bool_flags_mapped(self) -> None:
        db = parse_adsbexchange(_ADSBEX)
        assert db["ae292b"].get("mil") is True
        assert db["a11111"].get("pia") is True
        assert db["a22222"].get("ladd") is True

    def test_false_flags_omitted(self) -> None:
        db = parse_adsbexchange(_ADSBEX)
        # ac738e is mil/pia/ladd all false -> none of those keys present.
        assert "mil" not in db["ac738e"]
        assert "pia" not in db["ac738e"]
        assert "ladd" not in db["ac738e"]

    def test_string_fields_mapped(self) -> None:
        db = parse_adsbexchange(_ADSBEX)
        assert db["ae292b"]["type"] == "E6"
        assert db["ae292b"]["model"] == "E-6B Mercury"
        assert db["ac738e"]["ownop"] == "GILBERT WRIGHT"

    def test_bad_lines_skipped(self) -> None:
        raw = gzip.compress(b'not json\n{"icao":"ae0001","mil":true}\n\n')
        db = parse_adsbexchange(raw)
        assert db == {"ae0001": {"mil": True}}


# ---------------------------------------------------------------------------
# DatabaseStore
# ---------------------------------------------------------------------------


class TestDatabaseStore:
    def test_empty_by_default(self) -> None:
        store = DatabaseStore()
        assert store.current == {}
        assert store.lookup("ae292b") == {}

    def test_swap_is_atomic_reference(self) -> None:
        store = DatabaseStore()
        old = store.current
        store.swap({"ae292b": {"mil": True}})
        # The previously captured reference is unchanged (snapshot semantics).
        assert old == {}
        assert store.lookup("ae292b") == {"mil": True}


# ---------------------------------------------------------------------------
# DatabaseLoader
# ---------------------------------------------------------------------------


def _fetcher_from(mapping: dict[str, bytes]) -> Callable[[str], Awaitable[bytes]]:
    async def fetch(url: str) -> bytes:
        if url not in mapping:
            raise RuntimeError(f"no fake response for {url}")
        return mapping[url]

    return fetch


def _config(*names_urls: tuple[str, str]) -> DatabasesConfig:
    return DatabasesConfig(sources=[DatabaseSourceConfig(name=n, url=u) for n, u in names_urls])


class TestDatabaseLoader:
    async def test_refresh_merges_both_sources(self) -> None:
        store = DatabaseStore()
        loader = DatabaseLoader(
            _config(("mictronics", "mic://"), ("adsbexchange", "adsbex://")),
            store,
            fetcher=_fetcher_from({"mic://": _MIC, "adsbex://": _ADSBEX}),
        )
        ok = await loader.refresh_once()
        assert ok is True
        # ae292b is in both; merged carries fields from each.
        meta = store.lookup("ae292b")
        assert meta.get("mil") is True
        assert meta.get("model") == "E-6B Mercury"  # ADSBex
        # 004002 is Mictronics-only.
        assert store.lookup("004002")["reg"] == "Z-WPA"

    async def test_adsbex_wins_on_conflict(self) -> None:
        # Both sources define `type` for ae292b (Mictronics "E6", ADSBex "E6").
        # Craft a conflict: Mictronics says type X, ADSBex says type Y.
        mic = gzip.compress(b"ae0001;N1;OLD;10;Old Model;;;\n")
        adsbex = gzip.compress(
            json.dumps({"icao": "ae0001", "icaotype": "NEW", "model": "New", "mil": False}).encode()
            + b"\n"
        )
        store = DatabaseStore()
        loader = DatabaseLoader(
            _config(("mictronics", "m://"), ("adsbexchange", "a://")),
            store,
            fetcher=_fetcher_from({"m://": mic, "a://": adsbex}),
        )
        await loader.refresh_once()
        meta = store.lookup("ae0001")
        assert meta["type"] == "NEW"  # ADSBex wins
        assert meta["model"] == "New"
        assert meta["mil"] is True  # Mictronics mil survives (ADSBex had none)

    async def test_failed_refresh_keeps_previous(self) -> None:
        store = DatabaseStore()
        store.swap({"ae292b": {"mil": True}})  # a known-good prior copy

        async def failing(url: str) -> bytes:
            raise RuntimeError("network down")

        loader = DatabaseLoader(_config(("mictronics", "m://")), store, fetcher=failing)
        ok = await loader.refresh_once()
        assert ok is False
        assert store.lookup("ae292b") == {"mil": True}  # untouched

    async def test_one_source_down_other_still_loads(self) -> None:
        store = DatabaseStore()

        async def partial(url: str) -> bytes:
            if url == "mic://":
                return _MIC
            raise RuntimeError("adsbex down")

        loader = DatabaseLoader(
            _config(("mictronics", "mic://"), ("adsbexchange", "adsbex://")),
            store,
            fetcher=partial,
        )
        ok = await loader.refresh_once()
        assert ok is True
        assert store.lookup("004002")["reg"] == "Z-WPA"  # Mictronics loaded

    async def test_disabled_source_skipped(self) -> None:
        store = DatabaseStore()
        cfg = DatabasesConfig(
            sources=[
                DatabaseSourceConfig(name="mictronics", url="mic://", enabled=False),
            ]
        )
        loader = DatabaseLoader(cfg, store, fetcher=_fetcher_from({"mic://": _MIC}))
        ok = await loader.refresh_once()
        # Nothing enabled -> no successful source -> previous (empty) kept.
        assert ok is False
        assert store.current == {}

    async def test_unknown_source_skipped(self) -> None:
        store = DatabaseStore()
        loader = DatabaseLoader(
            _config(("mysteryDB", "x://")),
            store,
            fetcher=_fetcher_from({"x://": b"whatever"}),
        )
        ok = await loader.refresh_once()
        assert ok is False  # no known parser -> nothing loaded


# ---------------------------------------------------------------------------
# Live-shape sanity (no network): the real fixtures parse to expected counts
# ---------------------------------------------------------------------------


def test_fixtures_have_expected_shape() -> None:
    mic = parse_mictronics(_MIC)
    adsbex = parse_adsbexchange(_ADSBEX)
    assert len(mic) == 4
    assert len(adsbex) == 4
    # The marquee row: a real E-6B Mercury, military, in both DBs.
    assert mic["ae292b"]["mil"] is True
    assert adsbex["ae292b"]["mil"] is True
