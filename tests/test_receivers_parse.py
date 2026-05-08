"""Tests for parse_aircraft_json — the dump1090/readsb wire-format mapping.

In-memory dicts cover the field-by-field cases. The fixture-file flow
is exercised end-to-end in test_receivers_file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adsb_enrich.receivers._parse import parse_aircraft_json

_RX = "rx-home"
_BAND = "1090"


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Document-shape errors
# ---------------------------------------------------------------------------


class TestDocumentShape:
    def test_root_must_be_mapping(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            parse_aircraft_json([], receiver_name=_RX, band=_BAND, observed_at=_now())
        with pytest.raises(ValueError, match="must be a mapping"):
            parse_aircraft_json("string", receiver_name=_RX, band=_BAND, observed_at=_now())

    def test_aircraft_key_required(self) -> None:
        with pytest.raises(ValueError, match="aircraft"):
            parse_aircraft_json(
                {"now": 1.0, "messages": 0}, receiver_name=_RX, band=_BAND, observed_at=_now()
            )

    def test_aircraft_must_be_list(self) -> None:
        with pytest.raises(ValueError, match="aircraft"):
            parse_aircraft_json(
                {"aircraft": {"hex": "ae0001"}}, receiver_name=_RX, band=_BAND, observed_at=_now()
            )

    def test_empty_aircraft_list_returns_empty(self) -> None:
        observations, mps = parse_aircraft_json(
            {"aircraft": []}, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert observations == []
        assert mps is None


# ---------------------------------------------------------------------------
# Field mapping (happy path)
# ---------------------------------------------------------------------------


class TestFieldMapping:
    def test_full_record_maps_to_observation(self) -> None:
        payload = {
            "aircraft": [
                {
                    "hex": "ae0001",
                    "flight": "RCH171  ",
                    "alt_baro": 35000,
                    "alt_geom": 35100,
                    "lat": 30.33,
                    "lon": -97.99,
                    "gs": 480.5,
                    "track": 90.0,
                    "baro_rate": 0,
                    "category": "A4",
                    "nic": 8,
                    "nac_p": 10,
                    "rssi": -12.3,
                    "seen": 0.5,
                    "seen_pos": 0.5,
                    "nav_altitude_mcp": 35000,
                    "r": "12-9999",
                    "squawk": "1200",
                    "t": "C17",
                }
            ]
        }
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert len(observations) == 1
        obs = observations[0]
        assert obs.hex == "ae0001"
        assert obs.flight == "RCH171"
        assert obs.alt_baro_ft == 35000
        assert obs.alt_geom_ft == 35100
        assert obs.lat == 30.33
        assert obs.lon == -97.99
        assert obs.ground_speed_kt == 480.5
        assert obs.track_deg == 90.0
        assert obs.vertical_rate_fpm == 0
        assert obs.category == "A4"
        assert obs.nic == 8
        assert obs.nac_p == 10
        assert obs.rssi_dbfs == -12.3
        assert obs.seen_age_s == 0.5
        assert obs.seen_pos_age_s == 0.5
        assert obs.nav_altitude_mcp_ft == 35000
        assert obs.registration == "12-9999"
        assert obs.squawk == "1200"
        assert obs.aircraft_type == "C17"
        assert obs.is_tisb is False
        assert obs.on_ground is None

    def test_provenance_fields_copied(self) -> None:
        payload = {"aircraft": [{"hex": "ae0001"}]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        obs = observations[0]
        assert obs.seen_by == "rx-home"
        assert obs.band == "1090"
        assert obs.observed_at == _now()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_tisb_prefix_stripped_and_flagged(self) -> None:
        payload = {"aircraft": [{"hex": "~ae9999", "alt_baro": 30000}]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert len(observations) == 1
        obs = observations[0]
        assert obs.hex == "ae9999"
        assert obs.is_tisb is True

    def test_alt_baro_ground_becomes_on_ground(self) -> None:
        payload = {"aircraft": [{"hex": "ae0001", "alt_baro": "ground"}]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        obs = observations[0]
        assert obs.alt_baro_ft is None
        assert obs.on_ground is True

    def test_callsign_padded_with_spaces_stripped(self) -> None:
        payload = {"aircraft": [{"hex": "ae0001", "flight": "N12345  "}]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert observations[0].flight == "N12345"

    def test_callsign_whitespace_only_becomes_none(self) -> None:
        payload = {"aircraft": [{"hex": "ae0001", "flight": "        "}]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert observations[0].flight is None

    def test_uppercase_hex_normalized(self) -> None:
        payload = {"aircraft": [{"hex": "AE0001"}]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert observations[0].hex == "ae0001"

    def test_int_fields_accept_floats_too(self) -> None:
        # Some receivers emit numeric fields as floats even when the
        # schema is int-shaped. _get_int rejects those (preserves type
        # safety); _get_float accepts them.
        payload = {
            "aircraft": [
                {
                    "hex": "ae0001",
                    "alt_baro": 35000.7,  # float — should be rejected by int getter
                    "gs": 480,  # int — float getter accepts
                }
            ]
        }
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        obs = observations[0]
        assert obs.alt_baro_ft is None  # int getter rejected the float
        assert obs.ground_speed_kt == 480.0  # int promoted to float

    def test_bool_not_treated_as_int(self) -> None:
        # bool is a subclass of int in Python; explicit check ensures
        # `nic: True` does not become `nic=1`.
        payload = {"aircraft": [{"hex": "ae0001", "nic": True, "rssi": False}]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        obs = observations[0]
        assert obs.nic is None
        assert obs.rssi_dbfs is None


# ---------------------------------------------------------------------------
# Skipped records (silent)
# ---------------------------------------------------------------------------


class TestSilentSkips:
    def test_record_without_hex_skipped(self) -> None:
        payload = {
            "aircraft": [
                {"flight": "GHOST", "alt_baro": 25000},  # no hex
                {"hex": "ae0001"},
            ]
        }
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert len(observations) == 1
        assert observations[0].hex == "ae0001"

    def test_record_with_empty_hex_skipped(self) -> None:
        payload = {"aircraft": [{"hex": ""}, {"hex": "ae0001"}]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert len(observations) == 1

    def test_record_with_malformed_hex_skipped(self) -> None:
        payload = {
            "aircraft": [
                {"hex": "not-hex-at-all"},  # parse_hex raises
                {"hex": "ae0001"},
            ]
        }
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert len(observations) == 1
        assert observations[0].hex == "ae0001"

    def test_non_dict_record_skipped(self) -> None:
        payload = {"aircraft": ["not an object", {"hex": "ae0001"}, 42]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert len(observations) == 1

    def test_lone_tilde_hex_skipped(self) -> None:
        # parse_hex raises on "~" alone; record is skipped not exploding.
        payload = {"aircraft": [{"hex": "~"}, {"hex": "ae0001"}]}
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert len(observations) == 1
        assert observations[0].hex == "ae0001"


# ---------------------------------------------------------------------------
# Schema-drift tolerance
# ---------------------------------------------------------------------------


class TestSchemaDrift:
    def test_unknown_fields_ignored(self) -> None:
        # Future readsb / dump1090 forks add fields; we should not crash.
        payload = {
            "aircraft": [
                {
                    "hex": "ae0001",
                    "future_field_v3": {"some": "object"},
                    "another_unknown": [1, 2, 3],
                }
            ]
        }
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        assert len(observations) == 1

    def test_explicit_nulls_treated_as_missing(self) -> None:
        payload = {
            "aircraft": [
                {
                    "hex": "ae0001",
                    "flight": None,
                    "alt_baro": None,
                    "lat": None,
                    "rssi": None,
                }
            ]
        }
        observations, _ = parse_aircraft_json(
            payload, receiver_name=_RX, band=_BAND, observed_at=_now()
        )
        obs = observations[0]
        assert obs.flight is None
        assert obs.alt_baro_ft is None
        assert obs.lat is None
        assert obs.rssi_dbfs is None
        assert obs.on_ground is None
