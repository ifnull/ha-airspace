"""Receiver source implementations.

Public surface:

* ``ReceiverSource`` — abstract base; pull-based fetch() with built-in
  fail-fast + consecutive-failure tracking + Prometheus metric updates.
* ``FetchError`` — wraps every transient failure subclasses raise.
  The base class catches it, increments the failure counter, and yields
  an empty list. Anything that is NOT a FetchError propagates and the
  merger's poll loop crashes (which is the right outcome for a real bug
  vs a flaky receiver).
* ``HttpJsonReceiver`` — production receiver; polls a dump1090-style
  ``aircraft.json`` over HTTP with a long-lived ``httpx.AsyncClient``,
  configurable auth, and ``receiver.json`` location auto-discovery.
* ``FileReceiver`` — replays a captured ``aircraft.json`` from disk.
  Used by tests; also handy for offline diagnosis.
"""

from __future__ import annotations

from ha_airspace.receivers.base import FetchError, ReceiverSource
from ha_airspace.receivers.file import FileReceiver
from ha_airspace.receivers.http import HttpJsonReceiver

__all__ = [
    "FetchError",
    "FileReceiver",
    "HttpJsonReceiver",
    "ReceiverSource",
]
