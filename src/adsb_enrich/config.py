"""Configuration schema and loader.

Pydantic v2 models live at the system boundary (per CLAUDE.md). Once a
config has been validated, downstream code receives concrete values
with strict typing and never re-validates. Bad config exits the process
non-zero at startup with a field-path error.

Phase 1 surface: ``service``, ``watchpoints``, ``mqtt``, ``prometheus``,
``receivers``. Phase 2 will add ``databases`` (Phase 2a), ``journal``
(Phase 2b), ``photos`` (Phase 2c), ``enrichment``, and ``publish.alerts``
when those modules land. Strict mode rejects unknown top-level keys, so
copying a Phase 2 example into a Phase 1 install fails loudly — the
right behavior pre-public; Daniel iterates and the schema can break.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from adsb_enrich.models import Watchpoint


class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or fails validation.

    Wraps Pydantic ``ValidationError`` and YAML errors with the source
    file path so the entry point can print a single clear message and
    exit non-zero.
    """


# Reusable strict-mode setting. Applied to every model so adding a typo
# at any nesting depth produces a clear error rather than being silently
# ignored.
_STRICT = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Sub-section schemas
# ---------------------------------------------------------------------------


class ServiceConfig(BaseModel):
    """Top-level service knobs."""

    model_config = _STRICT

    poll_interval_s: float = Field(default=1.0, gt=0)
    """Default poll cadence. Receivers can override per-receiver."""

    http_timeout_s: float = Field(default=5.0, gt=0)
    """Per-request HTTP timeout for receiver fetches."""

    log_level: Literal["debug", "info", "warning", "error"] = "info"

    log_destination: Literal["stdout", "file"] = "stdout"
    """``stdout`` is captured by the HA add-on supervisor / journald /
    Docker. ``file`` is opt-in with size-capped rotation; only enable on
    systems where SD-card wear is not a concern."""


class WatchpointConfig(BaseModel):
    """A named geographic point alert rules and distance/bearing math
    reference.
    """

    model_config = _STRICT

    name: str = Field(..., min_length=1)
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    elevation_m: float | None = None

    def to_runtime(self) -> Watchpoint:
        """Convert to the runtime ``Watchpoint`` (frozen dataclass) used
        by the enricher's geometry pass and alert rule evaluation."""
        return Watchpoint(
            name=self.name,
            lat=self.lat,
            lon=self.lon,
            elevation_m=self.elevation_m,
        )


class MqttConfig(BaseModel):
    """MQTT broker connection + publish-throttle settings."""

    model_config = _STRICT

    broker: str = Field(..., min_length=1)
    port: int = Field(default=1883, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    base_topic: str = "adsb"
    discovery_prefix: str = "homeassistant"
    discovery_enabled: bool = True
    tls: bool = False

    publish_aircraft_min_interval_s: float = Field(default=1.0, ge=0)
    """Per-hex throttle for ``adsb/aircraft/<hex>`` publishes. Absorbs
    bursty receiver output without making HA wait."""

    publish_summary_min_interval_s: float = Field(default=1.0, ge=0)
    """Global throttle for ``adsb/summary/*`` publishes."""


class PrometheusConfig(BaseModel):
    """Optional ``/metrics`` HTTP exposition. Off by default; localhost-
    bound when on. Operators who want LAN-accessible metrics set
    ``bind`` explicitly (no auth on this endpoint).
    """

    model_config = _STRICT

    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = Field(default=9090, ge=1, le=65535)


class ReceiverLocationConfig(BaseModel):
    """Override for the receiver's self-reported location.

    Used when the receiver's ``receiver.json`` is absent or wrong (some
    appliances ship without one or with placeholder coordinates).
    """

    model_config = _STRICT

    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    alt_m: float | None = None


class AuthConfig(BaseModel):
    """Receiver auth. ``type: header`` lets users put arbitrary headers
    in (Cloudflare Access tokens, custom auth proxies, etc.) without
    needing a code change for each new auth scheme.
    """

    model_config = _STRICT

    type: Literal["none", "basic", "header"] = "none"
    username: str | None = None
    password: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_consistent_with_type(self) -> Self:
        if self.type == "basic" and (not self.username or not self.password):
            raise ValueError("auth.type='basic' requires both username and password to be set")
        if self.type == "header" and not self.headers:
            raise ValueError("auth.type='header' requires non-empty headers mapping")
        if self.type == "none" and (self.username or self.password or self.headers):
            raise ValueError("auth.type='none' must not set username, password, or headers")
        return self


class ReceiverConfig(BaseModel):
    """A single dump1090 / readsb / dump978-fa receiver source."""

    model_config = _STRICT

    name: str = Field(..., min_length=1)
    """Stable identifier used in MQTT topics (``adsb/receiver/<name>/...``)
    and metric labels. Renaming is a breaking change for downstream
    dashboards."""

    url: str = Field(..., min_length=1)
    """HTTP endpoint serving ``aircraft.json``."""

    band: Literal["1090", "978"]
    """No default. Silent miscategorization of a 978 receiver as 1090
    was the v0 footgun."""

    poll_interval_s: float | None = Field(default=None, gt=0)
    """Per-receiver poll cadence. ``None`` inherits from
    ``service.poll_interval_s``."""

    location: ReceiverLocationConfig | None = None
    """Override the receiver's self-reported location. ``None`` =
    auto-detect from the receiver's ``receiver.json``, falling back to
    no location if unavailable."""

    auth: AuthConfig = Field(default_factory=AuthConfig)
    enabled: bool = True


# ---------------------------------------------------------------------------
# Enrichment: flag rules (Phase 2a, slice 1)
# ---------------------------------------------------------------------------


class FlagConfig(BaseModel):
    """One declarative flag rule, identified by its single discriminator
    field. Exactly one of the matcher fields must be set:

    * ``sources`` — DB-backed: ``["mictronics:mil", "adsbexchange:mil"]``.
      The flag matches if any referenced ``db_metadata`` field is truthy.
      (Empty until the Phase-2a DB loader lands; never matches before then.)
    * ``squawks`` — transponder code in the list (e.g. emergency 7500/7600/7700).
    * ``patterns`` — callsign starts with any prefix.
    * ``types`` — ICAO type designator in the list (e.g. ``C17``).
    * ``categories`` — ADS-B emitter category in the list (e.g. ``A7`` rotorcraft).

    The matcher kind is inferred from which field is present; mixing two is a
    config error. Keeping one model (rather than a discriminated union keyed by
    an explicit ``type:``) means the YAML stays terse — the field name *is* the
    discriminator, matching DESIGN's examples.
    """

    model_config = _STRICT

    sources: list[str] | None = None
    squawks: list[str] | None = None
    patterns: list[str] | None = None
    types: list[str] | None = None
    categories: list[str] | None = None

    @model_validator(mode="after")
    def _exactly_one_matcher(self) -> Self:
        present = [
            name
            for name in ("sources", "squawks", "patterns", "types", "categories")
            if getattr(self, name) is not None
        ]
        if len(present) != 1:
            raise ValueError(
                "each flag must set exactly one of "
                "sources / squawks / patterns / types / categories; "
                f"got {present or 'none'}"
            )
        if not getattr(self, present[0]):
            raise ValueError(f"flag matcher '{present[0]}' must be a non-empty list")
        if present[0] == "sources":
            for ref in self.sources or []:
                if ref.count(":") != 1 or not all(ref.split(":")):
                    raise ValueError(
                        f"source ref must be 'db:field' (e.g. 'adsbexchange:mil'), got {ref!r}"
                    )
        return self

    @property
    def matcher(self) -> str:
        """Which discriminator this flag uses. Safe to read post-validation."""
        for name in ("sources", "squawks", "patterns", "types", "categories"):
            if getattr(self, name) is not None:
                return name
        raise AssertionError("validated FlagConfig always has one matcher")


class EnrichmentConfig(BaseModel):
    """Flag (and, slice 3, alert) rules. Absent section = no enrichment,
    which is the Phase 1 behavior. Flag names are arbitrary user labels
    (``military``, ``my_neighbor``) mapped to one matcher each."""

    model_config = _STRICT

    flags: dict[str, FlagConfig] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Reference databases (Phase 2a, slice 2)
# ---------------------------------------------------------------------------


class DatabaseSourceConfig(BaseModel):
    """One reference-database source. ``name`` selects the parser
    (``mictronics`` | ``adsbexchange``); an unrecognized name is skipped at
    load with a warning rather than failing validation, so the schema does
    not need updating to experiment with a mirror."""

    model_config = _STRICT

    name: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    enabled: bool = True


class DatabasesConfig(BaseModel):
    """Reference-database download + refresh settings. Absent section =
    no DB enrichment (``db_metadata`` stays empty; DB-backed flags never
    match) — the slice-1 behavior."""

    model_config = _STRICT

    cache_dir: str = "/data/db"
    refresh_interval_h: float = Field(default=168.0, gt=0)
    sources: list[DatabaseSourceConfig] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level Config
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Validated, fully-typed configuration root.

    Once constructed, downstream code reads attributes directly with no
    further validation. Use ``Config.poll_interval_for(receiver)`` to
    resolve the per-receiver cadence with service-level fallback.
    """

    model_config = _STRICT

    service: ServiceConfig = Field(default_factory=ServiceConfig)
    watchpoints: list[WatchpointConfig] = Field(..., min_length=1)
    mqtt: MqttConfig
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)
    receivers: list[ReceiverConfig] = Field(..., min_length=1)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    databases: DatabasesConfig = Field(default_factory=DatabasesConfig)

    @model_validator(mode="after")
    def _watchpoint_names_unique(self) -> Self:
        names = [wp.name for wp in self.watchpoints]
        if len(names) != len(set(names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"watchpoint names must be unique; duplicates: {duplicates}")
        return self

    @model_validator(mode="after")
    def _receiver_names_unique(self) -> Self:
        names = [r.name for r in self.receivers]
        if len(names) != len(set(names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"receiver names must be unique; duplicates: {duplicates}")
        return self

    def poll_interval_for(self, receiver: ReceiverConfig) -> float:
        """Resolve a receiver's poll cadence: receiver-level if set,
        else the service default. Always returns a positive float."""
        return (
            receiver.poll_interval_s
            if receiver.poll_interval_s is not None
            else self.service.poll_interval_s
        )

    def watchpoints_runtime(self) -> list[Watchpoint]:
        """Project the watchpoint configs into runtime ``Watchpoint``
        instances (frozen dataclasses) for the enricher."""
        return [wp.to_runtime() for wp in self.watchpoints]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(path: str | Path) -> Config:
    """Read and validate a YAML config file.

    Raises:
        ConfigError: file is missing, not parseable as YAML, the top-
            level value is not a mapping, or fails schema validation.
            The exception message includes the source file path and
            (for validation failures) the field path that failed.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in {path}: {exc}") from exc

    if data is None:
        raise ConfigError(f"{path}: config file is empty")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path}: top-level YAML value must be a mapping, got {type(data).__name__}"
        )

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid config\n{exc}") from exc


__all__ = [
    "AuthConfig",
    "Config",
    "ConfigError",
    "DatabaseSourceConfig",
    "DatabasesConfig",
    "EnrichmentConfig",
    "FlagConfig",
    "MqttConfig",
    "PrometheusConfig",
    "ReceiverConfig",
    "ReceiverLocationConfig",
    "ServiceConfig",
    "WatchpointConfig",
    "load_config",
]
