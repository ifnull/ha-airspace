"""Tests for FileReceiver — fixture-replay receiver.

End-to-end coverage from disk → parsed AircraftObservation. Captured
fixtures live in tests/fixtures/. Failure paths use temp files written
per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ha_airspace.models import ReceiverLocation
from ha_airspace.receivers.file import FileReceiver

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Happy path (real fixture files)
# ---------------------------------------------------------------------------


class TestBasicFixture:
    @pytest.fixture
    def receiver(self) -> FileReceiver:
        return FileReceiver(name="rx-home", band="1090", path=FIXTURES / "aircraft_basic.json")

    async def test_yields_two_aircraft(self, receiver: FileReceiver) -> None:
        observations = await receiver.fetch()
        assert len(observations) == 2

    async def test_first_aircraft_fields(self, receiver: FileReceiver) -> None:
        observations = await receiver.fetch()
        rch = next(o for o in observations if o.hex == "ae0001")
        assert rch.flight == "RCH171"
        assert rch.alt_baro_ft == 35000
        assert rch.lat == 30.33
        assert rch.lon == -75.99
        assert rch.ground_speed_kt == 480.5
        assert rch.category == "A4"
        assert rch.nic == 8
        assert rch.is_tisb is False
        assert rch.on_ground is None
        assert rch.seen_by == "rx-home"
        assert rch.band == "1090"

    async def test_ground_aircraft_marked_on_ground(self, receiver: FileReceiver) -> None:
        observations = await receiver.fetch()
        ground = next(o for o in observations if o.hex == "abc123")
        assert ground.on_ground is True
        assert ground.alt_baro_ft is None

    async def test_health_marks_online_after_success(self, receiver: FileReceiver) -> None:
        await receiver.fetch()
        h = await receiver.health()
        assert h["online"] is True
        assert h["aircraft_count"] == 2
        assert h["consecutive_failures"] == 0


class TestEdgeCasesFixture:
    """The edge_cases fixture exercises every silent-skip path plus the
    TIS-B prefix and whitespace-callsign normalization. The fixture has
    7 entries; only 2 should survive."""

    @pytest.fixture
    def receiver(self) -> FileReceiver:
        return FileReceiver(name="rx-home", band="1090", path=FIXTURES / "aircraft_edge_cases.json")

    async def test_skips_unusable_records(self, receiver: FileReceiver) -> None:
        observations = await receiver.fetch()
        # Survivors:
        #   - ~ae9999 (TIS-B)
        #   - AE0002 (uppercase, normalized)
        #   - ae0003 (full record)
        # Skipped:
        #   - no-hex GHOST
        #   - "not an object" string
        #   - "not-hex-at-all"
        #   - empty hex ""
        assert len(observations) == 3

    async def test_tisb_aircraft_flagged(self, receiver: FileReceiver) -> None:
        observations = await receiver.fetch()
        tisb = next(o for o in observations if o.hex == "ae9999")
        assert tisb.is_tisb is True

    async def test_uppercase_hex_normalized(self, receiver: FileReceiver) -> None:
        observations = await receiver.fetch()
        assert any(o.hex == "ae0002" for o in observations)

    async def test_whitespace_only_callsign_becomes_none(self, receiver: FileReceiver) -> None:
        observations = await receiver.fetch()
        ae2 = next(o for o in observations if o.hex == "ae0002")
        assert ae2.flight is None


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    async def test_missing_file_returns_empty_and_marks_unhealthy_after_threshold(
        self, tmp_path: Path
    ) -> None:
        rx = FileReceiver(name="rx-home", band="1090", path=tmp_path / "nope.json")
        for _ in range(3):
            result = await rx.fetch()
            assert result == []
        h = await rx.health()
        assert h["online"] is False
        assert h["consecutive_failures"] == 3

    async def test_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        rx = FileReceiver(name="rx-home", band="1090", path=path)
        result = await rx.fetch()
        assert result == []
        h = await rx.health()
        assert h["consecutive_failures"] == 1

    async def test_schema_drift_returns_empty(self, tmp_path: Path) -> None:
        # Valid JSON but wrong shape — no 'aircraft' array.
        path = tmp_path / "wrong-shape.json"
        path.write_text('{"hello": "world"}', encoding="utf-8")
        rx = FileReceiver(name="rx-home", band="1090", path=path)
        result = await rx.fetch()
        assert result == []
        h = await rx.health()
        assert h["consecutive_failures"] == 1

    async def test_path_is_directory_returns_empty(self, tmp_path: Path) -> None:
        # Reading a directory raises IsADirectoryError → caught as
        # OSError → wrapped in FetchError → returns [].
        rx = FileReceiver(name="rx-home", band="1090", path=tmp_path)
        result = await rx.fetch()
        assert result == []

    async def test_recovery_resets_failures(self, tmp_path: Path) -> None:
        path = tmp_path / "live.json"
        rx = FileReceiver(name="rx-home", band="1090", path=path)

        # Two failures (file does not exist yet).
        for _ in range(2):
            await rx.fetch()
        assert (await rx.health())["consecutive_failures"] == 2

        # Write a valid empty doc.
        path.write_text('{"aircraft": []}', encoding="utf-8")
        result = await rx.fetch()
        assert result == []
        assert (await rx.health())["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


class TestLocation:
    async def test_returns_none_when_not_supplied(self) -> None:
        rx = FileReceiver(name="rx-home", band="1090", path=FIXTURES / "aircraft_basic.json")
        assert await rx.location() is None

    async def test_returns_supplied_location(self) -> None:
        loc = ReceiverLocation(lat=30.33, lon=-75.99, alt_m=200.0, source="config")
        rx = FileReceiver(
            name="rx-home",
            band="1090",
            path=FIXTURES / "aircraft_basic.json",
            location=loc,
        )
        assert await rx.location() is loc
