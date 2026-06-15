"""Tests for ha_airspace.icao_country — ICAO hex -> country + flag emoji."""

from __future__ import annotations

import pytest

from ha_airspace.icao_country import country_for, flag_emoji, flag_for


class TestCountryFor:
    @pytest.mark.parametrize(
        ("hex_code", "iso2"),
        [
            ("a9558b", "us"),  # A-block US
            ("3c6444", "de"),  # Germany
            ("400a1b", "gb"),  # UK block, not a sub-territory
            ("c01234", "ca"),  # Canada
            ("7c1234", "au"),  # Australia
            ("4ca111", "ie"),  # Ireland
            ("e48abc", "br"),  # Brazil
        ],
    )
    def test_known_ranges(self, hex_code: str, iso2: str) -> None:
        assert country_for(hex_code) == iso2

    def test_uk_subterritory_resolves_before_catchall(self) -> None:
        # Bermuda / Isle of Man sit inside the 400000-43FFFF UK block; the
        # specific range must win over the gb catch-all.
        assert country_for("400050") == "bm"  # Bermuda 400000-4001BF
        assert country_for("424b10") == "im"  # Isle of Man 424B00-424BFF
        assert country_for("43eb00") == "gg"  # Guernsey 43EAFE-43EEFF

    def test_hong_kong_before_china(self) -> None:
        assert country_for("789abc") == "hk"  # specific
        assert country_for("780abc") == "cn"  # catch-all

    def test_tilde_prefix_stripped(self) -> None:
        assert country_for("~a9558b") == "us"

    @pytest.mark.parametrize("bad", [None, "", "zzz", "nothex", "f00100"])
    def test_unallocated_or_invalid_is_none(self, bad: str | None) -> None:
        # f00100 is in the ICAO temporary block (no country).
        assert country_for(bad) is None


class TestFlag:
    def test_flag_emoji_us(self) -> None:
        assert flag_emoji("us") == "🇺🇸"

    def test_flag_emoji_case_insensitive(self) -> None:
        assert flag_emoji("DE") == flag_emoji("de") == "🇩🇪"

    def test_flag_for_hex(self) -> None:
        assert flag_for("a9558b") == "🇺🇸"

    def test_flag_for_unallocated_none(self) -> None:
        assert flag_for("f00100") is None
        assert flag_for(None) is None
