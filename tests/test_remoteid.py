"""Tests for the Remote ID parser + RemoteIdHttpReceiver (Phase 3 slice 4).

The parser runs against fixtures captured from a real dump3411 feed (the
spoofed-serial drones with operator blocks, plus a position-less Basic-ID
drone and an empty feed). The receiver uses httpx.MockTransport — no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ha_airspace.receivers._parse import MessageRateTracker
from ha_airspace.receivers._remoteid_parse import parse_remoteid_json
from ha_airspace.receivers.remoteid import RemoteIdHttpReceiver

_FIXTURES = Path(__file__).parent / "fixtures"
_BASIC = json.loads((_FIXTURES / "remoteid_basic.json").read_text())
_EMPTY = json.loads((_FIXTURES / "remoteid_empty.json").read_text())


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Parser — document shape
# ---------------------------------------------------------------------------


class TestDocumentShape:
    def test_root_must_be_mapping(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            parse_remoteid_json([], receiver_name="d", observed_at=_now())

    def test_drones_key_required(self) -> None:
        with pytest.raises(ValueError, match="drones"):
            parse_remoteid_json({"now": 1.0}, receiver_name="d", observed_at=_now())

    def test_empty_feed_is_valid(self) -> None:
        obs, mps = parse_remoteid_json(_EMPTY, receiver_name="d", observed_at=_now())
        assert obs == []
        assert mps is None


# ---------------------------------------------------------------------------
# Parser — field mapping
# ---------------------------------------------------------------------------


class TestFieldMapping:
    def test_parses_all_drones(self) -> None:
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="dump3411", observed_at=_now())
        assert len(obs) == 3

    def test_identity_is_source_agnostic(self) -> None:
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="dump3411", observed_at=_now())
        drone = next(o for o in obs if o.track_id == "0x00")
        assert drone.track_id == "0x00"  # the UAS id, not a hex
        assert drone.hex is None
        assert drone.non_icao is True
        assert drone.band == "remoteid"
        assert drone.seen_by == "dump3411"

    def test_shared_position_fields_mapped(self) -> None:
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="dump3411", observed_at=_now())
        d = next(o for o in obs if o.track_id == "0x00")
        assert d.lat == 40.7128
        assert d.lon == -74.0060
        assert d.alt_geom_ft == 1276  # 1276.2 -> int (whole-ish)
        assert d.ground_speed_kt == 93.3
        assert d.track_deg == 0.0
        assert d.vertical_rate_fpm == 197
        assert d.rssi_dbfs == -5.0

    def test_drone_only_fields_on_drone_info(self) -> None:
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="dump3411", observed_at=_now())
        d = next(o for o in obs if o.track_id == "0x00")
        assert d.drone is not None
        assert d.drone.id_type == "serial"
        assert d.drone.ua_type == "multirotor"
        assert d.drone.agl_ft == 246.1
        assert d.drone.rid_source == "wifi_beacon"
        assert d.drone.self_id is None  # this drone broadcast no Self-ID

    def test_self_id_extracted(self) -> None:
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="dump3411", observed_at=_now())
        d = next(o for o in obs if o.track_id == "Spoofed_Serial_40456")
        assert d.drone is not None
        assert d.drone.self_id == "Spoofing test"

    def test_operator_block_extracted(self) -> None:
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="dump3411", observed_at=_now())
        d = next(o for o in obs if o.track_id == "0x00")
        assert d.drone is not None
        assert d.drone.operator_lat == 40.7165
        assert d.drone.operator_lon == -73.9990
        assert d.drone.operator_location_type == "live_gnss"
        assert d.drone.operator_alt_takeoff_ft == -3251.3

    def test_operator_location_type_takeoff(self) -> None:
        # Some transmitters report the drone's own launch point under the same
        # operator.lat/lon field, distinguished only by location_type — must not
        # be mistaken for a live operator fix.
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="dump3411", observed_at=_now())
        d = next(o for o in obs if o.track_id == "Spoofed_Serial_40456")
        assert d.drone is not None
        assert d.drone.operator_location_type == "takeoff"

    def test_operator_location_type_absent_when_no_operator_block(self) -> None:
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="dump3411", observed_at=_now())
        d = next(o for o in obs if o.track_id == "BASICID-NO-POS")
        assert d.drone is not None
        assert d.drone.operator_location_type is None

    def test_positionless_drone_kept(self) -> None:
        # Basic ID heard before any Location: valid, identity known, no position.
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="dump3411", observed_at=_now())
        d = next(o for o in obs if o.track_id == "BASICID-NO-POS")
        assert d.lat is None
        assert d.lon is None
        assert d.drone is not None
        assert d.drone.id_type == "session"
        # No operator message yet -> operator fields all None.
        assert d.drone.operator_lat is None

    def test_drone_without_id_skipped(self) -> None:
        payload = {"drones": [{"id_type": "serial", "lat": 1.0}, {"id": "ok"}]}
        obs, _ = parse_remoteid_json(payload, receiver_name="d", observed_at=_now())
        assert [o.track_id for o in obs] == ["ok"]

    def test_observed_at_is_ours_not_feed_now(self) -> None:
        obs, _ = parse_remoteid_json(_BASIC, receiver_name="d", observed_at=_now())
        assert all(o.observed_at == _now() for o in obs)


# ---------------------------------------------------------------------------
# Parser — messages_per_sec via the shared tracker
# ---------------------------------------------------------------------------


class TestRate:
    def test_rate_derived_across_polls(self) -> None:
        tracker = MessageRateTracker()
        first = {"now": 100.0, "messages": 400, "drones": []}
        second = {"now": 101.0, "messages": 520, "drones": []}
        _o1, mps1 = parse_remoteid_json(
            first, receiver_name="d", observed_at=_now(), rate_tracker=tracker
        )
        _o2, mps2 = parse_remoteid_json(
            second, receiver_name="d", observed_at=_now(), rate_tracker=tracker
        )
        assert mps1 is None
        assert mps2 == 120.0


# ---------------------------------------------------------------------------
# RemoteIdHttpReceiver — over a mock transport
# ---------------------------------------------------------------------------


def _receiver(payload: dict[str, object], *, status: int = 200) -> RemoteIdHttpReceiver:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return RemoteIdHttpReceiver(
        "dump3411",
        "http://drone.local:8754/data/remoteid.json",
        transport=httpx.MockTransport(handler),
    )


class TestReceiver:
    async def test_fetch_returns_drone_observations(self) -> None:
        rx = _receiver(_BASIC)
        obs = await rx.fetch()
        assert len(obs) == 3
        assert all(o.band == "remoteid" for o in obs)
        await rx.aclose()

    async def test_empty_feed_fetches_clean(self) -> None:
        rx = _receiver(_EMPTY)
        obs = await rx.fetch()
        assert obs == []
        h = await rx.health()
        assert h["online"] is True
        await rx.aclose()

    async def test_http_error_marks_unhealthy_after_threshold(self) -> None:
        rx = _receiver({}, status=503)
        for _ in range(3):  # UNHEALTHY_AFTER_FAILURES
            assert await rx.fetch() == []  # FetchError -> empty, counted
        h = await rx.health()
        assert h["online"] is False
        await rx.aclose()

    async def test_location_is_none(self) -> None:
        rx = _receiver(_EMPTY)
        assert await rx.location() is None
        await rx.aclose()

    async def test_band_is_remoteid(self) -> None:
        rx = _receiver(_EMPTY)
        assert rx.band == "remoteid"
        await rx.aclose()
