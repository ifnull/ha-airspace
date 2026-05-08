"""Receiver source implementations.

Public surface:

* ``ReceiverSource`` — abstract base; pull-based fetch() with built-in
  fail-fast + consecutive-failure tracking + Prometheus metric updates.
* ``FetchError`` — wraps every transient failure subclasses raise.
  The base class catches it, increments the failure counter, and yields
  an empty list. Anything that is NOT a FetchError propagates and the
  merger's poll loop crashes (which is the right outcome for a real bug
  vs a flaky receiver).
* ``FileReceiver`` — replays a captured ``aircraft.json`` from disk.
  Used by tests; also handy for offline diagnosis.

``HttpJsonReceiver`` lands in the next commit — it shares ``parse_aircraft_json``
from ``_parse`` with ``FileReceiver``.
"""

from __future__ import annotations

from adsb_enrich.receivers.base import FetchError, ReceiverSource
from adsb_enrich.receivers.file import FileReceiver

__all__ = [
    "FetchError",
    "FileReceiver",
    "ReceiverSource",
]
