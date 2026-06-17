"""Tests for ha_airspace.mqtt.payloads.

Cover:
  * Round-trip projection from internal types (AircraftState,
    ReceiverLocation, health() dict) to published payload.
  * Deterministic JSON output (sorted sets).
  * Datetime ISO 8601 serialization.
  * Phase-1-but-Phase-2-shape: empty flags / db_metadata don't break
    the contract.
  * extra="forbid": adding a field downstream without updating the
    payload model fails the test, not silently drops data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ha_airspace.models import (
    AircraftObservation,
    AircraftState,
    DroneInfo,
    ReceiverLocation,
)
from ha_airspace.mqtt.payloads import (
    PAYLOAD_SCHEMA_VERSION,
    AircraftPayload,
    AlertPayload,
    DronePayload,
    FlagAircraft,
    FlagFeedPayload,
    PhotoPayload,
    ReceiverLocationPayload,
    ReceiverStatsPayload,
)


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_observation(**overrides: object) -> AircraftObservation:
    defaults: dict[str, object] = {
        "hex": "ae0001",
        "observed_at": _now(),
        "seen_by": "rx-home",
        "band": "1090",
        "flight": "RCH171",
        "lat": 30.33,
        "lon": -75.99,
        "alt_baro_ft": 35000,
        "ground_speed_kt": 480.5,
        "track_deg": 90.0,
        "category": "A4",
        "nic": 8,
        "rssi_dbfs": -12.3,
    }
    defaults.update(overrides)
    return AircraftObservation(**defaults)  # type: ignore[arg-type]


def _make_state(**state_overrides: object) -> AircraftState:
    obs = _make_observation()
    state = AircraftState.from_first_observation(obs)
    for key, value in state_overrides.items():
        setattr(state, key, value)
    return state


def _make_drone_state() -> AircraftState:
    obs = AircraftObservation(
        track_id="1581F5BK000000000001",
        hex=None,
        non_icao=True,
        observed_at=_now(),
        seen_by="dump3411",
        band="remoteid",
        lat=30.34,
        lon=-75.98,
        alt_geom_ft=400,
        drone=DroneInfo(id_type="serial", agl_ft=300.0, operator_lat=30.33, operator_lon=-75.99),
    )
    return AircraftState.from_first_observation(obs)


# ---------------------------------------------------------------------------
# Contract: schema_version (Phase 4 lock)
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_aircraft_payload_carries_version(self) -> None:
        assert AircraftPayload.from_state(_make_state()).schema_version == PAYLOAD_SCHEMA_VERSION

    def test_drone_payload_carries_version(self) -> None:
        assert DronePayload.from_state(_make_drone_state()).schema_version == PAYLOAD_SCHEMA_VERSION

    def test_version_is_one_at_phase_4(self) -> None:
        assert PAYLOAD_SCHEMA_VERSION == 1

    def test_version_present_in_json(self) -> None:
        # Consumers branch on this before reading anything else, so it must be in
        # the serialized payload, not just the model.
        data = json.loads(AircraftPayload.from_state(_make_state()).model_dump_json())
        assert data["schema_version"] == 1


# ---------------------------------------------------------------------------
# AircraftPayload — projection from AircraftState
# ---------------------------------------------------------------------------


class TestAircraftPayload:
    def test_from_state_copies_canonical_fields(self) -> None:
        payload = AircraftPayload.from_state(_make_state())
        assert payload.hex == "ae0001"
        assert payload.flight == "RCH171"
        assert payload.lat == 30.33
        assert payload.lon == -75.99
        assert payload.alt_baro_ft == 35000
        assert payload.ground_speed_kt == 480.5
        assert payload.category == "A4"
        assert payload.is_tisb is False

    def test_from_state_copies_provenance(self) -> None:
        payload = AircraftPayload.from_state(_make_state())
        assert payload.bands == ["1090"]
        assert payload.seen_by == ["rx-home"]
        assert payload.first_seen == _now()
        assert payload.last_seen == _now()

    def test_from_state_serializes_sets_as_sorted_lists(self) -> None:
        # Multi-receiver / multi-band state — Phase 3 territory but
        # the payload shape supports it from Phase 1.
        state = _make_state()
        state.bands = {"978", "1090"}
        state.seen_by = {"rx-z", "rx-a", "rx-m"}
        payload = AircraftPayload.from_state(state)
        # Sorted: deterministic JSON for cache-aware consumers.
        assert payload.bands == ["1090", "978"]
        assert payload.seen_by == ["rx-a", "rx-m", "rx-z"]

    def test_from_state_includes_phase2_fields_as_empty(self) -> None:
        # Phase 1 doesn't populate flags/db_metadata, but the payload
        # surface includes them so Phase 2 doesn't change the contract.
        payload = AircraftPayload.from_state(_make_state())
        assert payload.flags == []
        assert payload.db_metadata == {}

    def test_from_state_includes_phase2_fields_when_populated(self) -> None:
        state = _make_state()
        state.flags = {"interesting", "military"}
        state.db_metadata = {"operator": "USAF", "type": "C-17"}
        payload = AircraftPayload.from_state(state)
        assert payload.flags == ["interesting", "military"]  # sorted
        assert payload.db_metadata == {"operator": "USAF", "type": "C-17"}

    def test_predictive_fields_default_to_none(self) -> None:
        payload = AircraftPayload.from_state(_make_state())
        assert payload.predicted_eta_to_home_s is None
        assert payload.predicted_closest_approach_nm is None

    def test_distance_and_bearing_per_watchpoint_preserved(self) -> None:
        state = _make_state()
        state.distance_to = {"home": 12.5, "office": 28.4}
        state.bearing_to = {"home": 270.0, "office": 90.0}
        payload = AircraftPayload.from_state(state)
        assert payload.distance_to == {"home": 12.5, "office": 28.4}
        assert payload.bearing_to == {"home": 270.0, "office": 90.0}

    def test_tisb_preserved(self) -> None:
        obs = _make_observation(is_tisb=True)
        state = AircraftState.from_first_observation(obs)
        payload = AircraftPayload.from_state(state)
        assert payload.is_tisb is True


class TestAircraftPayloadJson:
    def test_model_dump_json_produces_valid_json(self) -> None:
        payload = AircraftPayload.from_state(_make_state())
        text = payload.model_dump_json()
        # Re-parse to confirm valid JSON.
        parsed = json.loads(text)
        assert parsed["hex"] == "ae0001"
        assert parsed["flight"] == "RCH171"

    def test_datetime_serializes_to_iso_8601(self) -> None:
        payload = AircraftPayload.from_state(_make_state())
        text = payload.model_dump_json()
        parsed = json.loads(text)
        # Pydantic v2 default: ISO 8601 with explicit offset.
        assert parsed["first_seen"] == "2026-01-01T12:00:00Z"
        assert parsed["last_seen"] == "2026-01-01T12:00:00Z"

    def test_none_fields_serialize_as_null(self) -> None:
        # Aircraft without lat/lon (e.g., position not yet decoded)
        # should serialize null rather than dropping the keys, so HA
        # sees the field as unavailable rather than missing.
        obs = _make_observation(lat=None, lon=None, ground_speed_kt=None)
        state = AircraftState.from_first_observation(obs)
        payload = AircraftPayload.from_state(state)
        parsed = json.loads(payload.model_dump_json())
        assert parsed["lat"] is None
        assert parsed["lon"] is None
        assert parsed["ground_speed_kt"] is None

    def test_extra_fields_rejected_at_construction(self) -> None:
        # Strict-mode safety: if downstream adds a field to AircraftState
        # without updating the payload model, validation fails — caller
        # cannot accidentally smuggle untyped data through.
        with pytest.raises(ValidationError, match="extra"):
            AircraftPayload(
                hex="ae0001",
                bands=["1090"],
                seen_by=["rx-home"],
                first_seen=_now(),
                last_seen=_now(),
                distance_to={},
                bearing_to={},
                flags=[],
                db_metadata={},
                undocumented_field="surprise",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# ReceiverStatsPayload
# ---------------------------------------------------------------------------


class TestReceiverStatsPayload:
    def test_from_health_dict(self) -> None:
        health = {
            "online": True,
            "last_success": _now(),
            "consecutive_failures": 0,
            "aircraft_count": 12,
            "messages_per_sec": 240.5,
        }
        payload = ReceiverStatsPayload.from_health(health)
        assert payload.aircraft_count == 12
        assert payload.messages_per_sec == 240.5
        assert payload.online is True
        assert payload.last_success == _now()

    def test_from_health_with_no_success_yet(self) -> None:
        # Pre-first-poll state — health returns last_success=None.
        # Payload must serialize cleanly (None -> null in JSON).
        health = {
            "online": True,
            "last_success": None,
            "consecutive_failures": 0,
            "aircraft_count": 0,
            "messages_per_sec": 0.0,
        }
        payload = ReceiverStatsPayload.from_health(health)
        assert payload.last_success is None
        parsed = json.loads(payload.model_dump_json())
        assert parsed["last_success"] is None

    def test_unhealthy_state(self) -> None:
        health = {
            "online": False,
            "last_success": _now(),
            "consecutive_failures": 5,
            "aircraft_count": 0,
            "messages_per_sec": 0.0,
        }
        payload = ReceiverStatsPayload.from_health(health)
        assert payload.online is False
        assert payload.consecutive_failures == 5

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            ReceiverStatsPayload(
                aircraft_count=0,
                messages_per_sec=0.0,
                last_success=None,
                consecutive_failures=0,
                online=True,
                surprise="field",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# ReceiverLocationPayload
# ---------------------------------------------------------------------------


class TestReceiverLocationPayload:
    def test_from_runtime_with_full_location(self) -> None:
        loc = ReceiverLocation(lat=30.33, lon=-75.99, alt_m=200.0, source="receiver_json")
        payload = ReceiverLocationPayload.from_runtime(loc)
        assert payload.lat == 30.33
        assert payload.lon == -75.99
        assert payload.alt_m == 200.0
        assert payload.source == "receiver_json"

    def test_from_runtime_without_alt(self) -> None:
        loc = ReceiverLocation(lat=30.33, lon=-75.99, source="config")
        payload = ReceiverLocationPayload.from_runtime(loc)
        assert payload.alt_m is None

    def test_serialization_round_trip(self) -> None:
        loc = ReceiverLocation(lat=30.33, lon=-75.99, alt_m=200.0, source="config")
        payload = ReceiverLocationPayload.from_runtime(loc)
        text = payload.model_dump_json()
        parsed = json.loads(text)
        assert parsed == {
            "lat": 30.33,
            "lon": -75.99,
            "alt_m": 200.0,
            "source": "config",
        }


# ---------------------------------------------------------------------------
# AlertPayload — aircraft contract + optional photo (Phase 2c)
# ---------------------------------------------------------------------------


class TestAlertPayload:
    def test_carries_all_aircraft_fields_plus_schema_version(self) -> None:
        payload = AlertPayload.build(_make_state())
        assert payload.hex == "ae0001"
        assert payload.flight == "RCH171"
        assert payload.schema_version == PAYLOAD_SCHEMA_VERSION

    def test_photo_none_by_default(self) -> None:
        assert AlertPayload.build(_make_state()).photo is None

    def test_photo_injected(self) -> None:
        photo = PhotoPayload(
            thumbnail_url="https://t/img.jpg", link="https://p/1", photographer="Jane"
        )
        payload = AlertPayload.build(_make_state(), photo)
        assert payload.photo is not None
        assert payload.photo.thumbnail_url == "https://t/img.jpg"
        data = json.loads(payload.model_dump_json())
        assert data["photo"]["photographer"] == "Jane"
        assert data["schema_version"] == 1

    def test_wildcard_payload_photo_is_none(self) -> None:
        # AircraftPayload now carries an optional photo (shared with alerts +
        # the nearest summary), but the wildcard never looks one up: from_state
        # without a photo leaves it None, so the per-hex topic stays photo-free.
        payload = AircraftPayload.from_state(_make_state())
        assert payload.photo is None
        data = json.loads(payload.model_dump_json())
        assert data["photo"] is None

    def test_from_state_attaches_photo_when_supplied(self) -> None:
        photo = PhotoPayload(thumbnail_url="https://x/p.jpg", photographer="A. Smith")
        payload = AircraftPayload.from_state(_make_state(), photo)
        assert payload.photo is not None
        assert payload.photo.thumbnail_url == "https://x/p.jpg"

    def test_from_state_sets_country_and_flag(self) -> None:
        # _make_state uses hex ae0001 -> US (A-block).
        payload = AircraftPayload.from_state(_make_state())
        assert payload.country == "us"
        assert payload.country_flag == "🇺🇸"

    def test_entity_picture_mirrors_photo_for_ha_map(self) -> None:
        photo = PhotoPayload(thumbnail_url="https://x/p.jpg")
        assert AircraftPayload.from_state(_make_state(), photo).entity_picture == "https://x/p.jpg"
        assert AircraftPayload.from_state(_make_state()).entity_picture is None


# ---------------------------------------------------------------------------
# HA-map aliases: latitude/longitude mirror lat/lon (dashboard polish)
# ---------------------------------------------------------------------------


class TestMapAliases:
    def test_aircraft_latitude_longitude_mirror_lat_lon(self) -> None:
        payload = AircraftPayload.from_state(_make_state())
        assert payload.latitude == payload.lat == 30.33
        assert payload.longitude == payload.lon == -75.99

    def test_drone_latitude_longitude_mirror_lat_lon(self) -> None:
        payload = DronePayload.from_state(_make_drone_state())
        assert payload.latitude == payload.lat == 30.34
        assert payload.longitude == payload.lon == -75.98

    def test_aliases_in_json_for_map_card(self) -> None:
        data = json.loads(AircraftPayload.from_state(_make_state()).model_dump_json())
        assert data["latitude"] == 30.33
        assert data["longitude"] == -75.99


# ---------------------------------------------------------------------------
# DronePayload db_metadata (FAA make/model enrichment)
# ---------------------------------------------------------------------------


class TestDronePayloadDbMetadata:
    def test_empty_by_default(self) -> None:
        assert DronePayload.from_state(_make_drone_state()).db_metadata == {}

    def test_carries_faa_fields(self) -> None:
        state = _make_drone_state()
        state.db_metadata = {"make": "DJI", "model": "Mavic 3", "status": "accepted"}
        payload = DronePayload.from_state(state)
        assert payload.db_metadata["make"] == "DJI"
        data = json.loads(payload.model_dump_json())
        assert data["db_metadata"]["model"] == "Mavic 3"


# ---------------------------------------------------------------------------
# FlagAircraft / FlagFeedPayload (per-flag feed sensors)
# ---------------------------------------------------------------------------


class TestFlagFeedPayload:
    def test_row_projects_compact_fields(self) -> None:
        state = _make_state()
        state.flags = {"military", "heavy"}
        state.distance_to = {"home": 12.5, "office": 28.4}
        state.bearing_to = {"home": 270.0, "office": 90.0}
        row = FlagAircraft.from_state(state, watchpoint="home")
        assert row.hex == "ae0001"
        assert row.flight == "RCH171"
        assert row.alt_baro_ft == 35000
        assert row.distance_nm == 12.5  # home, not office
        assert row.bearing_deg == 270.0
        assert row.flags == ["heavy", "military"]  # sorted

    def test_row_carries_country_flag(self) -> None:
        row = FlagAircraft.from_state(_make_state(), watchpoint="home")
        assert row.country_flag == "🇺🇸"  # hex ae0001 -> US

    def test_row_carries_db_metadata_for_why(self) -> None:
        # db_metadata rides each row so a flag table can show *why* (PIA/LADD/mil).
        state = _make_state()
        state.db_metadata = {"ladd": True, "ownop": "ACME"}
        row = FlagAircraft.from_state(state, watchpoint="home")
        assert row.db_metadata == {"ladd": True, "ownop": "ACME"}
        assert json.loads(row.model_dump_json())["db_metadata"]["ladd"] is True

    def test_row_distance_none_when_watchpoint_absent(self) -> None:
        state = _make_state()
        state.distance_to = {}
        state.bearing_to = {}
        row = FlagAircraft.from_state(state, watchpoint="home")
        assert row.distance_nm is None
        assert row.bearing_deg is None

    def test_feed_carries_count_and_version(self) -> None:
        state = _make_state()
        feed = FlagFeedPayload(
            flag="military",
            count=3,
            watchpoint="home",
            aircraft=[FlagAircraft.from_state(state, watchpoint="home")],
        )
        assert feed.schema_version == PAYLOAD_SCHEMA_VERSION
        data = json.loads(feed.model_dump_json())
        assert data["flag"] == "military"
        assert data["count"] == 3  # true total, may exceed len(aircraft)
        assert len(data["aircraft"]) == 1
        assert data["photo"] is None  # defaults None (nearest-match photo, opt-in)

    def test_feed_carries_nearest_match_photo(self) -> None:
        feed = FlagFeedPayload(
            flag="military",
            count=1,
            watchpoint="home",
            aircraft=[FlagAircraft.from_state(_make_state(), watchpoint="home")],
            photo=PhotoPayload(thumbnail_url="https://x/p.jpg", photographer="Jane"),
        )
        data = json.loads(feed.model_dump_json())
        assert data["photo"]["thumbnail_url"] == "https://x/p.jpg"
