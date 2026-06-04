"""Tests for the multi-source merger core (Phase 3 slice 1).

Pure, no IO. Covers canonical selection for every DESIGN §3 tiebreaker rung,
plus the Merger's seen_by/bands accumulation, last_seen advance, and the
freshness window that excludes a lagging receiver's frozen snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ha_airspace.merger import Merger, select_canonical
from ha_airspace.models import AircraftObservation

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _obs(
    *,
    seen_by: str,
    band: str = "1090",
    at: datetime = _T0,
    hex_code: str = "ae0001",
    nic: int | None = None,
    nac_p: int | None = None,
    seen_pos: float | None = None,
    rssi: float | None = None,
    lat: float | None = 30.4,
    lon: float | None = -97.9,
) -> AircraftObservation:
    return AircraftObservation(
        hex=hex_code,
        observed_at=at,
        seen_by=seen_by,
        band=band,
        lat=lat,
        lon=lon,
        nic=nic,
        nac_p=nac_p,
        seen_pos_age_s=seen_pos,
        rssi_dbfs=rssi,
    )


# ---------------------------------------------------------------------------
# select_canonical — the locked DESIGN §3 order
# ---------------------------------------------------------------------------


class TestSelectCanonical:
    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            select_canonical([])

    def test_single_observation(self) -> None:
        o = _obs(seen_by="rx-a")
        assert select_canonical([o]) is o

    def test_fresh_position_beats_stale(self) -> None:
        fresh = _obs(seen_by="rx-a", seen_pos=1.0, nic=4)
        stale = _obs(seen_by="rx-b", seen_pos=20.0, nic=11)  # better NIC but stale
        # Freshness (rung 1) dominates NIC (rung 2).
        assert select_canonical([stale, fresh]) is fresh

    def test_higher_nic_wins_among_fresh(self) -> None:
        lo = _obs(seen_by="rx-a", seen_pos=1.0, nic=6)
        hi = _obs(seen_by="rx-b", seen_pos=2.0, nic=9)
        assert select_canonical([lo, hi]) is hi

    def test_higher_nac_p_breaks_nic_tie(self) -> None:
        a = _obs(seen_by="rx-a", seen_pos=1.0, nic=8, nac_p=7)
        b = _obs(seen_by="rx-b", seen_pos=1.0, nic=8, nac_p=10)
        assert select_canonical([a, b]) is b

    def test_freshest_position_breaks_quality_tie(self) -> None:
        a = _obs(seen_by="rx-a", seen_pos=3.0, nic=8, nac_p=9)
        b = _obs(seen_by="rx-b", seen_pos=0.5, nic=8, nac_p=9)
        assert select_canonical([a, b]) is b

    def test_rssi_breaks_remaining_tie(self) -> None:
        a = _obs(seen_by="rx-a", seen_pos=1.0, nic=8, nac_p=9, rssi=-20.0)
        b = _obs(seen_by="rx-b", seen_pos=1.0, nic=8, nac_p=9, rssi=-5.0)
        assert select_canonical([a, b]) is b  # stronger (less negative) RSSI

    def test_receiver_name_final_deterministic_tiebreak(self) -> None:
        # Identical quality -> alphabetical receiver name decides, every time.
        a = _obs(seen_by="rx-alpha", seen_pos=1.0, nic=8, nac_p=9, rssi=-10.0)
        b = _obs(seen_by="rx-bravo", seen_pos=1.0, nic=8, nac_p=9, rssi=-10.0)
        assert select_canonical([a, b]) is a
        assert select_canonical([b, a]) is a  # order-independent

    def test_none_quality_sorts_worst(self) -> None:
        full = _obs(seen_by="rx-a", seen_pos=1.0, nic=8, nac_p=9)
        bare = _obs(seen_by="rx-b", seen_pos=1.0)  # no nic/nac_p
        assert select_canonical([bare, full]) is full


# ---------------------------------------------------------------------------
# Merger — accumulation across receivers
# ---------------------------------------------------------------------------


class TestMerger:
    def test_first_observation_creates_state(self) -> None:
        m = Merger()
        state = m.ingest(_obs(seen_by="rx-a"))
        assert state.hex == "ae0001"
        assert state.seen_by == {"rx-a"}
        assert state.bands == {"1090"}
        assert state.canonical_source == "rx-a"

    def test_second_receiver_accumulates_seen_by(self) -> None:
        m = Merger()
        m.ingest(_obs(seen_by="rx-a", seen_pos=2.0, nic=6))
        state = m.ingest(_obs(seen_by="rx-b", seen_pos=1.0, nic=9))
        assert state.seen_by == {"rx-a", "rx-b"}
        # rx-b has fresher + higher NIC -> canonical.
        assert state.canonical_source == "rx-b"

    def test_band_merge_same_hex(self) -> None:
        m = Merger()
        m.ingest(_obs(seen_by="rx-1090", band="1090"))
        state = m.ingest(_obs(seen_by="rx-978", band="978"))
        assert state.bands == {"1090", "978"}

    def test_last_seen_advances_to_newest(self) -> None:
        m = Merger()
        m.ingest(_obs(seen_by="rx-a", at=_T0))
        later = _T0 + timedelta(seconds=3)
        state = m.ingest(_obs(seen_by="rx-b", at=later))
        assert state.last_seen == later

    def test_last_seen_not_moved_backwards(self) -> None:
        m = Merger()
        later = _T0 + timedelta(seconds=3)
        m.ingest(_obs(seen_by="rx-a", at=later))
        state = m.ingest(_obs(seen_by="rx-b", at=_T0))  # older observation
        assert state.last_seen == later

    def test_lagging_receiver_excluded_from_canonical(self) -> None:
        # rx-stale has great quality but its snapshot is 30s behind the freshest
        # observation -> excluded from the selection window; rx-fresh wins.
        m = Merger(canonical_window_s=5.0)
        stale_at = _T0
        fresh_at = _T0 + timedelta(seconds=30)
        m.ingest(_obs(seen_by="rx-stale", at=stale_at, seen_pos=0.1, nic=11, nac_p=11))
        state = m.ingest(_obs(seen_by="rx-fresh", at=fresh_at, seen_pos=2.0, nic=5))
        # rx-stale still recorded for diagnostics...
        assert state.seen_by == {"rx-stale", "rx-fresh"}
        # ...but not chosen as canonical despite better quality.
        assert state.canonical_source == "rx-fresh"

    def test_within_window_best_quality_wins(self) -> None:
        # Both observations within the window -> quality decides, not recency.
        m = Merger(canonical_window_s=5.0)
        m.ingest(_obs(seen_by="rx-a", at=_T0, seen_pos=1.0, nic=5))
        state = m.ingest(_obs(seen_by="rx-b", at=_T0 + timedelta(seconds=2), seen_pos=1.0, nic=9))
        assert state.canonical_source == "rx-b"

    def test_remove_drops_state(self) -> None:
        m = Merger()
        m.ingest(_obs(seen_by="rx-a"))
        m.remove("ae0001")
        assert "ae0001" not in m.states
        m.remove("ae0001")  # idempotent

    def test_distinct_hexes_independent(self) -> None:
        m = Merger()
        m.ingest(_obs(seen_by="rx-a", hex_code="ae0001"))
        m.ingest(_obs(seen_by="rx-a", hex_code="ae0002"))
        assert set(m.states) == {"ae0001", "ae0002"}
