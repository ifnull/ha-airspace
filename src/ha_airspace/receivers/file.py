"""FileReceiver — replays a captured aircraft.json from disk.

Two use cases:

1. **Tests**: drive the merger / publisher / enricher with deterministic
   inputs. No network, no flakiness, no fixtures inside the receiver
   class itself — just point at a JSON file.
2. **Offline diagnosis**: capture a real receiver's output during an
   unusual airspace event (military exercise, inbound from offshore,
   etc.) and replay it later to test rule changes against real data.

Reads sync (``Path.read_text``) on every fetch — files are tiny (~50KB
typical aircraft.json) and the disk read is microseconds. Async file
I/O via aiofiles would be premature here; if profiling ever shows it
matters, swap.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ha_airspace.metrics import MetricsRegistry
from ha_airspace.models import AircraftObservation, ReceiverLocation
from ha_airspace.receivers._parse import MessageRateTracker, parse_aircraft_json
from ha_airspace.receivers.base import FetchError, ReceiverSource


class FileReceiver(ReceiverSource):
    """Replay an aircraft.json from disk on every fetch.

    The same file is re-read each call (so editing it between fetches
    in tests is a valid pattern). If the file is missing or malformed,
    ``fetch()`` returns ``[]`` and ``health()`` reports unhealthy after
    the threshold — same semantics as a flaky network receiver.
    """

    def __init__(
        self,
        name: str,
        band: str,
        path: str | Path,
        *,
        location: ReceiverLocation | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        super().__init__(name, band, metrics=metrics)
        self._path = Path(path)
        self._location = location
        self._rate_tracker = MessageRateTracker()

    async def _do_fetch(
        self,
    ) -> tuple[list[AircraftObservation], float | None]:
        """Read + parse the fixture file. Wrap every transient failure
        in ``FetchError`` so the base class counts it correctly."""
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise FetchError(f"fixture file missing: {self._path}") from exc
        except OSError as exc:
            raise FetchError(f"could not read fixture file {self._path}: {exc}") from exc

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise FetchError(f"malformed JSON in {self._path}: {exc}") from exc

        observed_at = datetime.now(UTC)
        try:
            return parse_aircraft_json(
                payload,
                receiver_name=self.name,
                band=self.band,
                observed_at=observed_at,
                rate_tracker=self._rate_tracker,
            )
        except ValueError as exc:
            # parse_aircraft_json raises only on schema-shape failures
            # (no 'aircraft' array, wrong root). Treat as transient —
            # a corrupt fixture should not crash the merger any more
            # than a corrupt network response would.
            raise FetchError(f"schema drift in {self._path}: {exc}") from exc

    async def location(self) -> ReceiverLocation | None:
        """Static location supplied at construction. None if not given."""
        return self._location
