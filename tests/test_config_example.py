"""The shipped config.example.yaml must always validate.

A broken example is a broken first-run experience. This pins it to the real
loader so any schema change that the example doesn't keep up with fails here
instead of in a user's terminal.
"""

from __future__ import annotations

from pathlib import Path

from adsb_enrich.config import load_config

_EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def test_example_config_exists() -> None:
    assert _EXAMPLE.is_file(), "config.example.yaml is missing from the repo root"


def test_example_config_validates() -> None:
    config = load_config(_EXAMPLE)
    # Sanity: the documented defaults survive the round-trip.
    assert config.mqtt.base_topic == "adsb"
    assert [w.name for w in config.watchpoints] == ["home"]
    assert config.receivers[0].band == "1090"
    assert config.receivers[0].enabled is True
