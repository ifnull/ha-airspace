"""Drift guard for the shipped HA automation blueprints.

A blueprint is a public compatibility surface: once a user imports it and
builds automations on top, a renamed attribute breaks them silently — the
template just renders empty, with no error anywhere. Nothing in HA validates
`state_attr(e, 'foo')` against what we actually publish.

So these tests tie the blueprint's templates back to the code that produces
the attributes. Rename a key in `_alert_info` and the blueprint that reads it
fails here rather than in someone's notification three weeks later.

Same intent as test_addon_render.py, which stops the add-on options schema and
the service config schema from drifting apart.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

from ha_airspace.models import AircraftObservation, AircraftState
from ha_airspace.mqtt.payloads import PhotoPayload
from ha_airspace.mqtt.publisher import _alert_info

_BLUEPRINT_DIR = Path(__file__).parent.parent / "blueprints" / "automation" / "ha-airspace"
_ALERT_BLUEPRINT = _BLUEPRINT_DIR / "alert_notification.yaml"


class _InputLoader(yaml.SafeLoader):
    """SafeLoader that understands HA's `!input` tag, which SafeLoader rejects."""


_InputLoader.add_constructor(
    "!input", lambda loader, node: {"__input__": loader.construct_scalar(node)}
)


def _load(path: Path) -> dict[str, Any]:
    # _InputLoader subclasses SafeLoader, so this is not an unsafe load.
    return yaml.load(path.read_text(), Loader=_InputLoader)


def _published_alert_attributes() -> set[str]:
    """Every attribute key the alert `info` topic can carry, from the real
    producer — photo included, since the blueprint offers a photo option."""
    obs = AircraftObservation(
        hex="ae0001",
        observed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        seen_by="rx-home",
        band="1090",
        flight="RCH171",
        lat=30.33,
        lon=-75.99,
        alt_baro_ft=35000,
    )
    state = AircraftState.from_first_observation(obs)
    photo = PhotoPayload(
        thumbnail_url="https://example.invalid/t.jpg",
        photographer="A Photographer",
        link="https://example.invalid/p",
    )
    return set(_alert_info(state, photo))


def _referenced_attributes(path: Path) -> set[str]:
    """Attribute names the blueprint reads via `state_attr(<anything>, 'name')`."""
    return set(re.findall(r"state_attr\(\s*[^,]+,\s*['\"]([^'\"]+)['\"]\s*\)", path.read_text()))


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_alert_blueprint_exists() -> None:
    assert _ALERT_BLUEPRINT.is_file(), f"missing blueprint: {_ALERT_BLUEPRINT}"


def test_alert_blueprint_is_a_valid_automation_blueprint() -> None:
    doc = _load(_ALERT_BLUEPRINT)
    assert doc["blueprint"]["domain"] == "automation"
    for key in ("name", "description", "source_url", "input"):
        assert key in doc["blueprint"], f"blueprint metadata missing {key!r}"
    # HA requires a trigger and an action at the top level of the automation.
    assert doc["trigger"], "blueprint has no trigger"
    assert doc["action"], "blueprint has no action"


def test_every_declared_input_is_used() -> None:
    """An input the body never references is a UI field that silently does
    nothing — the most confusing possible blueprint bug."""
    doc = _load(_ALERT_BLUEPRINT)
    declared = set(doc["blueprint"]["input"])
    used = set(re.findall(r"!input\s+(\w+)", _ALERT_BLUEPRINT.read_text()))
    assert declared == used, (
        f"declared-but-unused: {declared - used}; used-but-undeclared: {used - declared}"
    )


# ---------------------------------------------------------------------------
# The actual drift guard
# ---------------------------------------------------------------------------


def test_referenced_attributes_are_actually_published() -> None:
    referenced = _referenced_attributes(_ALERT_BLUEPRINT)
    assert referenced, "no state_attr() references found — did the template shape change?"
    published = _published_alert_attributes()
    missing = referenced - published
    assert not missing, (
        f"blueprint reads attributes the alert info topic does not publish: {sorted(missing)}. "
        f"Published keys are {sorted(published)} (see publisher._alert_info)."
    )


@pytest.mark.parametrize("attribute", ["flight", "hex", "distance_to", "bearing_to"])
def test_core_attributes_stay_published(attribute: str) -> None:
    """The fields the blueprint's message line cannot render without. Kept as
    an explicit list so removing one fails loudly even if the blueprint is
    edited in the same commit."""
    assert attribute in _published_alert_attributes()


def test_watchpoint_keyed_attributes_are_dicts() -> None:
    """The blueprint indexes distance_to/bearing_to by watchpoint name. If
    these ever flatten to scalars, `.get(watchpoint)` silently yields nothing."""
    info = _alert_info(
        AircraftState.from_first_observation(
            AircraftObservation(
                hex="ae0001",
                observed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
                seen_by="rx-home",
                band="1090",
                lat=30.33,
                lon=-75.99,
            )
        ),
        None,
    )
    assert isinstance(info["distance_to"], dict)
    assert isinstance(info["bearing_to"], dict)


def test_photo_attributes_only_present_with_a_photo() -> None:
    """The blueprint's include_photo path degrades to a plain notification when
    the airframe has no photo, which relies on entity_picture being absent."""
    state = AircraftState.from_first_observation(
        AircraftObservation(
            hex="ae0001",
            observed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
            seen_by="rx-home",
            band="1090",
        )
    )
    assert "entity_picture" not in _alert_info(state, None)
    assert "entity_picture" in _published_alert_attributes()


# ---------------------------------------------------------------------------
# Template compilation
# ---------------------------------------------------------------------------


def _templates(doc: Any, path: str = "") -> list[tuple[str, str]]:
    """Every (yaml-path, string) in the document that contains Jinja."""
    found: list[tuple[str, str]] = []
    if isinstance(doc, dict):
        for key, value in doc.items():
            found += _templates(value, f"{path}.{key}")
    elif isinstance(doc, list):
        for index, value in enumerate(doc):
            found += _templates(value, f"{path}[{index}]")
    elif isinstance(doc, str) and ("{{" in doc or "{%" in doc):
        found.append((path.lstrip("."), doc))
    return found


def test_every_jinja_template_compiles() -> None:
    """HA renders these with Jinja2, which is *not* Python: `dict(**a, **b)`
    raises "invalid syntax for function call expression", and `{% set %}`
    inside `{% if %}` does not escape the block scope without a namespace.
    Both were hit writing this blueprint; neither shows up as anything but an
    empty notification at runtime.
    """
    env = jinja2.Environment()
    failures: list[str] = []
    templates = _templates(_load(_ALERT_BLUEPRINT))
    assert templates, "no Jinja found — did the blueprint stop templating?"
    for where, source in templates:
        try:
            env.parse(source)
        except jinja2.TemplateSyntaxError as exc:
            failures.append(f"{where}: {exc.message}")
    assert not failures, "invalid Jinja in blueprint:\n" + "\n".join(failures)


def test_message_template_renders_with_everything_missing() -> None:
    """Every attribute is optional in practice — an aircraft broadcasting only
    a hex must still produce a sane message, not "None · None nm"."""
    env = jinja2.Environment()
    doc = _load(_ALERT_BLUEPRINT)
    message = doc["action"][0]["data"]["message"]
    rendered = env.from_string(message).render(
        country_flag=None,
        flight="ae0001",
        aircraft_type=None,
        registration=None,
        distance=None,
        bearing=None,
        altitude=None,
    )
    assert "None" not in rendered
    assert "ae0001" in rendered


def test_message_template_renders_with_everything_present() -> None:
    env = jinja2.Environment()
    doc = _load(_ALERT_BLUEPRINT)
    rendered = env.from_string(doc["action"][0]["data"]["message"]).render(
        country_flag="\U0001f1fa\U0001f1f8",
        flight="RCH171",
        aircraft_type="C17",
        registration="00-0171",
        distance=12.34,
        bearing=271.6,
        altitude=35000,
    )
    for fragment in ("RCH171", "C17", "00-0171", "12.3 nm", "272", "35000 ft"):
        assert fragment in rendered, f"{fragment!r} missing from: {rendered}"
