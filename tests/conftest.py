"""Shared test fixtures.

Time is a fixture (CLAUDE.md): tests never use real wall-clock time. Use the
`frozen_time` fixture (freezegun) for deterministic datetime, or pass a Clock
protocol into the unit under test where the abstraction makes sense.

Network is forbidden in unit tests (CLAUDE.md). Anything requiring a real
broker, real receiver, or real outbound HTTP is an integration test under
``tests/integration/`` with the ``integration`` marker, which is skipped by
default. Run integration tests with ``uv run pytest -m integration``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from freezegun import freeze_time
from freezegun.api import (
    FrozenDateTimeFactory,
    StepTickTimeFactory,
    TickingDateTimeFactory,
)

# Union of factories freezegun may hand back depending on its construction kwargs.
# Tests typically only need .tick() / .move_to(), which are common to all three.
TimeFactory = FrozenDateTimeFactory | StepTickTimeFactory | TickingDateTimeFactory


@pytest.fixture
def frozen_time() -> Iterator[TimeFactory]:
    """Freeze time at a deterministic instant.

    Yields the freezegun factory so tests can advance the clock with
    ``frozen_time.tick(seconds=N)`` when needed.
    """
    with freeze_time(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)) as frozen:
        yield frozen
