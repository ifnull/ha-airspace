"""ReceiverSource abstract base + shared fail-fast / health-tracking machinery.

Pull-based interface (locked decision from /plan-eng-review D2):
the merger owns polling cadence and calls ``fetch()`` per receiver on
its own timer. ``fetch()`` does ONE request, never retries internally,
and returns ``[]`` on any transient failure. The base class catches
``FetchError`` from the subclass-implemented ``_do_fetch()``, counts
consecutive failures, and exposes them via ``health()`` for the merger
to mark the receiver unhealthy after the threshold.

What lives here vs. in subclasses:

* **Base class**: error catch, failure counting, health() shape,
  Prometheus metric updates. All shared.
* **Subclass**: ``_do_fetch()`` (the actual HTTP / disk / synthetic
  request) and ``location()``. The wire-format parsing is shared via
  ``parse_aircraft_json`` in ``_parse.py``.

Subclasses MUST raise ``FetchError`` (not a generic Exception) for
known transient failures. Anything else propagates and is treated as
a real bug — a 500 stack trace beats silent corruption.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

import structlog

from ha_airspace.metrics import MetricsRegistry
from ha_airspace.models import AircraftObservation, ReceiverLocation

log = structlog.get_logger(__name__)


class FetchError(Exception):
    """Transient receiver failure. Subclasses raise this to signal that
    the merger should keep going (mark unhealthy after threshold) rather
    than crash. Always chain with ``from`` to preserve the underlying
    exception (httpx, json decode, schema drift, etc.) for log context.
    """


class ReceiverSource(ABC):
    """Abstract base for every receiver implementation.

    Concrete subclasses must implement:

    * ``async _do_fetch() -> tuple[list[AircraftObservation], float | None]``
      — one request; returns observations and the receiver's reported
      messages-per-second (or None if not available). Raise ``FetchError``
      on transient failure.
    * ``async location() -> ReceiverLocation | None`` — one-shot fetch
      of the receiver's self-reported location. Cached by the caller.
    """

    UNHEALTHY_AFTER_FAILURES: int = 3
    """Consecutive failures before ``health()`` reports ``online=False``.
    Locked from /plan-eng-review D2: enough that one cosmic-ray network
    blip does not page the user, few enough that a really-down receiver
    surfaces within ~3 seconds at 1 Hz."""

    def __init__(
        self,
        name: str,
        band: str,
        *,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        """Construct the base.

        Args:
            name: Stable receiver identifier (used in MQTT topics and
                metric labels). Must match the value in config.
            band: ``"1090"`` or ``"978"``. No default — silent
                miscategorization is the v0 footgun.
            metrics: Optional MetricsRegistry. If supplied, ``fetch()``
                automatically updates the per-receiver counters/gauges.
                If None, no metric updates (useful in unit tests that
                do not care about Prometheus state).
        """
        self.name = name
        self.band = band
        self._metrics = metrics

        # Health-tracking state.
        self._consecutive_failures: int = 0
        self._last_success: datetime | None = None
        self._last_aircraft_count: int = 0
        self._last_messages_per_sec: float = 0.0

    @abstractmethod
    async def _do_fetch(
        self,
    ) -> tuple[list[AircraftObservation], float | None]:
        """Subclass hook: perform a single fetch.

        Returns:
            ``(observations, messages_per_sec)`` on success.
            ``messages_per_sec`` is the receiver's reported decoded-
            message rate when known; ``None`` if not.

        Raises:
            FetchError: on any transient failure (timeout, connect
                error, malformed JSON, schema drift). The base class
                catches and turns it into ``[]`` + a failure tick.
        """

    @abstractmethod
    async def location(self) -> ReceiverLocation | None:
        """Subclass hook: fetch the receiver's self-reported location.

        Called once at startup; result cached by the caller. Return
        ``None`` if the receiver does not advertise a location.
        """

    async def fetch(self) -> list[AircraftObservation]:
        """One poll cycle. Fail-fast, no retry.

        On success: resets consecutive-failure counter, updates the
        last-success timestamp and aircraft count, increments the
        ``status="ok"`` poll counter, sets per-receiver gauges.

        On ``FetchError``: increments consecutive failures, increments
        the ``status="fail"`` poll counter, returns ``[]``. The merger
        sees an empty list and treats it like a poll with no aircraft;
        ``health()`` reflects the failure for the publisher to surface
        on the receiver-status MQTT topic.

        Anything that is NOT a ``FetchError`` propagates — it is a real
        bug, not a flaky receiver, and a stack trace is the right
        signal.
        """
        try:
            observations, messages_per_sec = await self._do_fetch()
        except FetchError as exc:
            return self._record_failure(exc)
        return self._record_success(observations, messages_per_sec)

    async def health(self) -> dict[str, Any]:
        """Diagnostic snapshot. Required keys (locked schema):

        * ``online: bool`` — False after ``UNHEALTHY_AFTER_FAILURES``
          consecutive failures; flips back True on the next success.
        * ``last_success: datetime | None`` — UTC timestamp.
        * ``consecutive_failures: int`` — resets on first success.
        * ``aircraft_count: int`` — from the last successful fetch.
        * ``messages_per_sec: float`` — from the last successful fetch.

        Subclasses may add extra keys. The merger and publisher only
        depend on the required ones.
        """
        return {
            "online": self._consecutive_failures < self.UNHEALTHY_AFTER_FAILURES,
            "last_success": self._last_success,
            "consecutive_failures": self._consecutive_failures,
            "aircraft_count": self._last_aircraft_count,
            "messages_per_sec": self._last_messages_per_sec,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _record_success(
        self,
        observations: list[AircraftObservation],
        messages_per_sec: float | None,
    ) -> list[AircraftObservation]:
        self._consecutive_failures = 0
        self._last_success = datetime.now(UTC)
        self._last_aircraft_count = len(observations)
        self._last_messages_per_sec = messages_per_sec if messages_per_sec is not None else 0.0
        if self._metrics is not None:
            self._metrics.receiver_polls.labels(receiver=self.name, status="ok").inc()
            self._metrics.receiver_aircraft_visible.labels(receiver=self.name).set(
                self._last_aircraft_count
            )
            self._metrics.receiver_messages_per_second.labels(receiver=self.name).set(
                self._last_messages_per_sec
            )
            self._metrics.receiver_consecutive_failures.labels(receiver=self.name).set(0)
        return observations

    def _record_failure(self, exc: FetchError) -> list[AircraftObservation]:
        self._consecutive_failures += 1
        cause = exc.__cause__ or exc
        log.warning(
            "receiver_fetch_failed",
            receiver=self.name,
            error_class=type(cause).__name__,
            error_msg=str(exc),
            consecutive_failures=self._consecutive_failures,
            unhealthy=self._consecutive_failures >= self.UNHEALTHY_AFTER_FAILURES,
        )
        if self._metrics is not None:
            self._metrics.receiver_polls.labels(receiver=self.name, status="fail").inc()
            self._metrics.receiver_consecutive_failures.labels(receiver=self.name).set(
                self._consecutive_failures
            )
        return []
