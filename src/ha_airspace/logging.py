"""structlog configuration.

One function, ``configure_logging``, called once at startup before anything
logs. Two renderers:

* **console** — colored, human-readable key=value lines for a TTY / dev.
* **json** — one JSON object per line for production (HA add-on supervisor,
  journald, Docker, log shippers). This is the default.

The choice is driven by config (``service.log_destination``: ``stdout`` keeps
the JSON renderer; a dev opts into console explicitly) and the level by
``service.log_level``. structlog writes to stdout either way — capture is the
supervisor's job, per DESIGN.md.

stdlib ``logging`` is bridged into structlog so libraries that use it
(httpx, asyncio) flow through the same renderer and level filter rather than
printing their own unstructured lines.
"""

from __future__ import annotations

import logging
import sys

import structlog

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def configure_logging(*, level: str = "info", renderer: str = "json") -> None:
    """Configure structlog + stdlib logging for the process.

    Args:
        level: One of ``debug`` | ``info`` | ``warning`` | ``error``.
            Unknown values fall back to ``info`` (defensive — config
            validation already constrains this, but startup logging must
            never itself raise).
        renderer: ``json`` (default, production) or ``console`` (dev TTY).

    Idempotent enough for tests: each call fully re-applies configuration.
    """
    log_level = _LEVELS.get(level, logging.INFO)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    final: structlog.typing.Processor
    if renderer == "console":
        final = structlog.dev.ConsoleRenderer()
    else:
        # Render any exc_info to a `exception` field, then emit JSON.
        shared_processors.append(structlog.processors.format_exc_info)
        final = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, final],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging (httpx, asyncio, etc.) to the same level so
    # third-party libraries do not bypass our filter with their own handlers.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    # httpx/httpcore emit one INFO line per request ("HTTP Request: GET ...").
    # At our ~1 Hz per-receiver polling that floods the console, so cap them at
    # WARNING unless the operator explicitly asked for debug. Set explicitly in
    # both cases so the result is idempotent across calls (these loggers are
    # process-global and otherwise retain a previous call's level).
    noisy_level = log_level if log_level <= logging.DEBUG else logging.WARNING
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(noisy_level)


def renderer_for(log_destination: str) -> str:
    """Map ``service.log_destination`` to a renderer name.

    ``stdout`` (default) -> ``json`` for machine-parseable production logs;
    ``file`` (opt-in, dev/diagnostics) -> ``console`` for readability. The
    file-rotation handler itself is a later concern; this just picks the
    renderer.
    """
    return "console" if log_destination == "file" else "json"


__all__ = ["configure_logging", "renderer_for"]
