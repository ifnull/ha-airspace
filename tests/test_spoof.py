"""Tests for ha_airspace.spoof.SpoofDetector (Tier-1 Remote ID spoof flag).

Deterministic, no clock. Covers the two Tier-1 signals (malformed serial,
self_id shared across distinct serials), the non-drone / non-serial skips, the
cross-track self_id index and its pruning on self_id change and on forget.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ha_airspace.config import SpoofConfig
from ha_airspace.models import AircraftObservation, AircraftState, DroneInfo
from ha_airspace.spoof import SPOOF_FLAG, SpoofDetector, _is_malformed_serial

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _detector() -> SpoofDetector:
    return SpoofDetector(SpoofConfig(enabled=True))


def _drone(
    track_id: str,
    *,
    id_type: str = "serial",
    self_id: str | None = None,
) -> AircraftState:
    obs = AircraftObservation(
        track_id=track_id,
        hex=None,
        non_icao=True,
        observed_at=_T0,
        seen_by="dump3411",
        band="remoteid",
        lat=30.34,
        lon=-75.98,
        drone=DroneInfo(id_type=id_type, self_id=self_id),
    )
    return AircraftState.from_first_observation(obs)


def _aircraft() -> AircraftState:
    obs = AircraftObservation(hex="ae0001", observed_at=_T0, seen_by="rx", band="1090")
    return AircraftState.from_first_observation(obs)


class TestMalformedSerial:
    def test_real_serial_not_malformed(self) -> None:
        # Genuine ANSI/CTA-2063-A serials (the user's real captures).
        for serial in ("1581F8LQC25810024UXM", "1581F11VKJ8B00204D80", "1581F67QE243C00A008A"):
            assert _is_malformed_serial("serial", serial) is False

    def test_placeholder_serial_is_malformed(self) -> None:
        assert _is_malformed_serial("serial", "0x00") is True  # len 4 < 6
        assert _is_malformed_serial("serial", "") is True
        assert _is_malformed_serial("serial", "AB-CD-EF") is True  # non-alphanumeric
        assert _is_malformed_serial("serial", "X" * 21) is True  # too long

    def test_only_serials_are_judged(self) -> None:
        # session / utm_uuid / caa_reg have their own formats; not flagged here.
        assert _is_malformed_serial("session", "0x00") is False
        assert _is_malformed_serial("utm_uuid", "anything") is False

    def test_flag_added_for_malformed(self) -> None:
        det = _detector()
        state = _drone("0x00")
        det.update(state)
        assert SPOOF_FLAG in state.flags

    def test_no_flag_for_clean_lone_drone(self) -> None:
        det = _detector()
        state = _drone("1581F8LQC25810024UXM", self_id="inspection")
        det.update(state)
        assert SPOOF_FLAG not in state.flags


class TestSharedSelfId:
    def test_same_self_id_two_serials_flagged(self) -> None:
        det = _detector()
        a = _drone("1581F8LQC25810024UXM", self_id="Spoofing test")
        b = _drone("1581F11VKJ8B00204D80", self_id="Spoofing test")
        det.update(a)  # first sighting: count 1, not yet shared
        det.update(b)  # second distinct serial with same self_id -> flagged
        assert SPOOF_FLAG in b.flags
        # a is re-evaluated next poll and also trips (index now has both).
        a2 = _drone("1581F8LQC25810024UXM", self_id="Spoofing test")
        det.update(a2)
        assert SPOOF_FLAG in a2.flags

    def test_distinct_self_ids_not_flagged(self) -> None:
        det = _detector()
        a = _drone("1581F8LQC25810024UXM", self_id="survey")
        b = _drone("1581F11VKJ8B00204D80", self_id="delivery")
        det.update(a)
        det.update(b)
        assert SPOOF_FLAG not in a.flags
        assert SPOOF_FLAG not in b.flags

    def test_empty_self_id_never_shared(self) -> None:
        det = _detector()
        a = _drone("1581F8LQC25810024UXM", self_id=None)
        b = _drone("1581F11VKJ8B00204D80", self_id=None)
        det.update(a)
        det.update(b)
        assert SPOOF_FLAG not in a.flags
        assert SPOOF_FLAG not in b.flags

    def test_self_id_change_unindexes_old(self) -> None:
        det = _detector()
        a = _drone("serialAAAAAA1", self_id="shared")
        b = _drone("serialBBBBBB2", self_id="shared")
        det.update(a)
        det.update(b)  # both share "shared"
        # a re-broadcasts a *different* self_id: it should no longer count toward
        # "shared", so a lone remaining b is not flagged on the old string.
        a2 = _drone("serialAAAAAA1", self_id="moved")
        det.update(a2)
        b2 = _drone("serialBBBBBB2", self_id="shared")
        det.update(b2)
        assert SPOOF_FLAG not in b2.flags

    def test_forget_prunes_index(self) -> None:
        det = _detector()
        a = _drone("serialAAAAAA1", self_id="shared")
        b = _drone("serialBBBBBB2", self_id="shared")
        det.update(a)
        det.update(b)
        det.forget("serialAAAAAA1")  # a departs
        b2 = _drone("serialBBBBBB2", self_id="shared")
        det.update(b2)
        assert SPOOF_FLAG not in b2.flags  # only b left -> no longer shared

    def test_forget_is_idempotent(self) -> None:
        det = _detector()
        det.forget("never-seen")  # must not raise


class TestNonDrone:
    def test_aircraft_ignored(self) -> None:
        det = _detector()
        state = _aircraft()
        det.update(state)  # no drone block, no remoteid band
        assert SPOOF_FLAG not in state.flags
