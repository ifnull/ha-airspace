"""High-level publisher: topic routing + throttle + retention policy.

Sits on top of ``MqttClient``. Knows the Phase 1 topic taxonomy
(aircraft / summary / receiver / status / discovery), the retention
policy (state-bearing topics retained, transient signals not), and
the throttle policy (per-hex aircraft, global summary). The merger
calls these methods; the publisher translates calls into ``client.publish``
with the right topic, payload, retain, and topic_class.

Throttling uses ``time.monotonic`` by default (immune to wall-clock
adjustments) and is injectable via the ``clock`` arg for deterministic
tests.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import structlog

from ha_airspace.config import Config
from ha_airspace.icao_country import country_for, flag_for
from ha_airspace.metrics import MetricsRegistry
from ha_airspace.models import AircraftState, ReceiverLocation
from ha_airspace.mqtt.client import MqttClient
from ha_airspace.mqtt.discovery import build_discovery_payloads
from ha_airspace.mqtt.payloads import (
    AircraftPayload,
    AlertPayload,
    DronePayload,
    FlagFeedPayload,
    PhotoPayload,
    ReceiverLocationPayload,
    ReceiverStatsPayload,
)

log = structlog.get_logger(__name__)


class Publisher:
    """Topic-aware MQTT publish surface.

    Construction args:
      client: An MqttClient — call ``publish_aircraft(...)``, etc., and
        the calls go through its publish queue.
      config: Validated app config. Provides the base_topic, the
        throttle intervals, and the discovery prefix.
      metrics: Optional MetricsRegistry, kept for symmetry with other
        modules; the actual publish counters are owned by MqttClient.
      clock: Override ``time.monotonic`` for deterministic throttle
        tests. Returns float seconds.
    """

    def __init__(
        self,
        client: MqttClient,
        config: Config,
        *,
        metrics: MetricsRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._config = config
        self._metrics = metrics
        self._clock = clock

        self._base = config.mqtt.base_topic
        self._aircraft_min_interval = config.mqtt.publish_aircraft_min_interval_s
        self._summary_min_interval = config.mqtt.publish_summary_min_interval_s

        # Throttle state. 0.0 sentinel always allows the first publish
        # since (now - 0.0) > any finite min_interval. Per-track publishes
        # (aircraft + drones) share one dict — track_id is unique across both.
        self._last_aircraft_publish: dict[str, float] = {}
        self._last_summary_publish: float = 0.0
        self._last_drone_summary_publish: float = 0.0

        # Last published receiver status string, per receiver. The status topic
        # is retained and changes rarely (online/unhealthy/offline), so we only
        # publish on transition — not once per poll. Cleared on (re)connect so a
        # broker that dropped its retained state gets a fresh republish.
        self._last_receiver_status: dict[str, str] = {}

        # Whether the last "nearest" publish was the empty-retained sentinel
        # (no aircraft/drone in range). Re-publishing empty bytes every tick
        # is both wasteful and spams HA's MQTT integration with "Erroneous
        # JSON" warnings once per publish interval, since it tries to
        # json-decode state_topic payloads before the value_template runs.
        # We only need to send the clearing empty payload once, on the
        # transition into "empty" — not on every subsequent tick while it
        # stays empty. Cleared on (re)connect, same reasoning as
        # _last_receiver_status: a broker that dropped its retained state
        # needs the clearing payload re-sent even though our in-process view
        # never changed.
        self._nearest_was_empty: bool = False
        self._nearest_drone_was_empty: bool = False

    # ------------------------------------------------------------------
    # On-connect hook (status:online + discovery republish)
    # ------------------------------------------------------------------

    async def on_connect(self, *, sw_version: str | None = None) -> None:
        """Run after every successful broker connect.

        Order matters:
          1. Publish ``airspace/status: online`` retained, so HA flips the
             availability sensors before discovery payloads land.
          2. Publish the full discovery payload set (idempotent on
             retained topics; protects against the broker losing
             retained state on a restart).

        The merger wires this into ``MqttClient.on_connect`` at startup.
        """
        # Forget per-receiver status, and whether nearest/nearest_drone were
        # already latched empty, so the next poll republishes them — the
        # broker may have lost its retained state across this (re)connect.
        self._last_receiver_status.clear()
        self._nearest_was_empty = False
        self._nearest_drone_was_empty = False
        await self._client.publish(
            f"{self._base}/status",
            b"online",
            retain=True,
            topic_class="status",
        )
        for topic, body in build_discovery_payloads(self._config, sw_version=sw_version):
            await self._client.publish(
                topic,
                json.dumps(body, separators=(",", ":")).encode("utf-8"),
                retain=True,
                topic_class="discovery",
            )

    # ------------------------------------------------------------------
    # Per-aircraft topic — power-user wildcard, NOT auto-discovered
    # ------------------------------------------------------------------

    async def publish_aircraft(self, state: AircraftState) -> bool:
        """Publish per-track state to ``airspace/aircraft/<track_id>``.

        The topic key is the merge key — ICAO hex for ADS-B, UAS id for
        Remote ID — so a track is one topic regardless of source. Throttled
        per-track by ``mqtt.publish_aircraft_min_interval_s``. Returns True if
        published, False if suppressed by throttle.
        """
        now = self._clock()
        last = self._last_aircraft_publish.get(state.track_id, 0.0)
        if (now - last) < self._aircraft_min_interval:
            return False
        self._last_aircraft_publish[state.track_id] = now

        payload = AircraftPayload.from_state(state).model_dump_json()
        await self._client.publish(
            f"{self._base}/aircraft/{state.track_id}",
            payload,
            retain=True,
            topic_class="aircraft",
        )
        return True

    async def purge_aircraft(self, track_id: str) -> None:
        """Clear the retained aircraft topic when a track is PURGED.

        Empty payload + retain=True tells the broker to drop the
        retained value. Forgetting this leaves zombie aircraft in HA
        forever (CLAUDE.md "things that will trip you up").
        """
        await self._client.publish(
            f"{self._base}/aircraft/{track_id}",
            b"",
            retain=True,
            topic_class="aircraft",
        )
        self._last_aircraft_publish.pop(track_id, None)

    # ------------------------------------------------------------------
    # Alert topics — per-rule/per-hex detail + per-rule active flag
    # ------------------------------------------------------------------

    async def publish_alert(
        self, rule: str, state: AircraftState, *, photo: PhotoPayload | None = None
    ) -> None:
        """Publish ``airspace/alert/<rule>/<track_id>`` on rule ENTER: the
        triggering track's full state, retained so a late subscriber sees the
        active alert. Cleared by ``clear_alert`` on EXIT.

        ``photo`` (Planespotters, Phase 2c) is injected here only — the alert
        payload, never the wildcard aircraft topic. Also refreshes the per-rule
        ``info`` topic with this (latest-triggering) track's summary, which the
        HA ``binary_sensor`` exposes as attributes (incl. ``entity_picture``)
        for a picture/glance alert card."""
        payload = AlertPayload.build(state, photo).model_dump_json()
        await self._client.publish(
            f"{self._base}/alert/{rule}/{state.track_id}",
            payload,
            retain=True,
            topic_class="alert",
        )
        await self._client.publish(
            f"{self._base}/alert/{rule}/info",
            json.dumps(_alert_info(state, photo), separators=(",", ":")).encode("utf-8"),
            retain=True,
            topic_class="alert",
        )

    async def clear_alert(self, rule: str, track_id: str) -> None:
        """Clear ``airspace/alert/<rule>/<track_id>`` on rule EXIT (empty-retained),
        so the alert does not linger in HA after the track stops matching."""
        await self._client.publish(
            f"{self._base}/alert/{rule}/{track_id}",
            b"",
            retain=True,
            topic_class="alert",
        )

    async def publish_alert_active(self, rule: str, *, active: bool) -> None:
        """Publish ``airspace/alert/<rule>/active`` = ``on`` | ``off`` retained.
        The per-rule HA ``binary_sensor`` reads this directly (payload_on=on),
        turning on while any aircraft matches the rule and off when none do.

        When the rule goes inactive, clear the ``info`` attribute topic too, so
        the picture/glance card doesn't keep showing a departed aircraft."""
        await self._client.publish(
            f"{self._base}/alert/{rule}/active",
            b"on" if active else b"off",
            retain=True,
            topic_class="alert",
        )
        if not active:
            await self._client.publish(
                f"{self._base}/alert/{rule}/info",
                b"",
                retain=True,
                topic_class="alert",
            )

    # ------------------------------------------------------------------
    # Drone topics — Remote ID tracks, separate from aircraft
    # ------------------------------------------------------------------

    async def publish_drone(self, state: AircraftState) -> bool:
        """Publish per-drone state to ``airspace/drone/<track_id>`` (the UAS id).

        Throttled per-track by the aircraft min-interval (drones and aircraft
        share the per-track throttle dict; track_ids are unique across both).
        Returns True if published, False if suppressed by throttle."""
        now = self._clock()
        last = self._last_aircraft_publish.get(state.track_id, 0.0)
        if (now - last) < self._aircraft_min_interval:
            return False
        self._last_aircraft_publish[state.track_id] = now

        payload = DronePayload.from_state(state).model_dump_json()
        await self._client.publish(
            f"{self._base}/drone/{state.track_id}",
            payload,
            retain=True,
            topic_class="drone",
        )
        return True

    async def purge_drone(self, track_id: str) -> None:
        """Clear the retained drone topic when a drone track is PURGED
        (empty-retained), so a departed drone does not linger in HA."""
        await self._client.publish(
            f"{self._base}/drone/{track_id}",
            b"",
            retain=True,
            topic_class="drone",
        )
        self._last_aircraft_publish.pop(track_id, None)

    async def publish_drone_summary(self, *, count: int, nearest: AircraftState | None) -> bool:
        """Publish ``airspace/summary/{drone_count, nearest_drone}``. Globally
        throttled like the aircraft summary. ``nearest=None`` clears the
        nearest-drone topic (empty-retained) so HA goes unavailable rather
        than showing a stale drone once the sky is clear of them."""
        now = self._clock()
        if (now - self._last_drone_summary_publish) < self._summary_min_interval:
            return False
        self._last_drone_summary_publish = now

        await self._client.publish(
            f"{self._base}/summary/drone_count",
            str(count).encode("utf-8"),
            retain=True,
            topic_class="summary",
        )
        if nearest is not None:
            nearest_payload: bytes | str = DronePayload.from_state(nearest).model_dump_json()
            self._nearest_drone_was_empty = False
            await self._client.publish(
                f"{self._base}/summary/nearest_drone",
                nearest_payload,
                retain=True,
                topic_class="summary",
            )
        elif not self._nearest_drone_was_empty:
            # First tick with no drone in range: clear the retained topic once.
            self._nearest_drone_was_empty = True
            await self._client.publish(
                f"{self._base}/summary/nearest_drone",
                b"",
                retain=True,
                topic_class="summary",
            )
        return True

    # ------------------------------------------------------------------
    # Summary topics — the surface HA actually subscribes to
    # ------------------------------------------------------------------

    async def publish_summary(
        self,
        *,
        count: int,
        nearest: AircraftState | None,
        count_by_flag: dict[str, int] | None = None,
        flag_feeds: dict[str, FlagFeedPayload] | None = None,
        nearest_photo: PhotoPayload | None = None,
    ) -> bool:
        """Publish ``airspace/summary/{count, nearest, count_by_flag}`` plus a
        ``summary/by_flag/<flag>`` topic for each entry in ``flag_feeds``.

        Globally throttled (one summary publish per ``mqtt.publish_summary_min_interval_s``).
        Returns True if published, False if throttled.

        ``nearest=None`` publishes empty-retained on the nearest topic
        so HA's Nearest Aircraft sensor goes unavailable rather than
        showing stale data when the airspace empties.

        ``flag_feeds`` is published retained per flag (a bounded, distance-sorted
        list of matching aircraft) backing the per-flag ``sensor.airspace_flag_*``
        entities. Empty feeds are still published (count 0, empty list) so the
        sensor reads 0 rather than going unavailable when nothing matches.

        ``nearest_photo`` (when supplied) is attached to the nearest payload only
        — a single, throttled entity, so the lookup cost is bounded, unlike the
        per-hex wildcard which never carries a photo.
        """
        now = self._clock()
        if (now - self._last_summary_publish) < self._summary_min_interval:
            return False
        self._last_summary_publish = now

        await self._client.publish(
            f"{self._base}/summary/count",
            str(count).encode("utf-8"),
            retain=True,
            topic_class="summary",
        )

        if nearest is not None:
            nearest_payload: bytes | str = AircraftPayload.from_state(
                nearest, nearest_photo
            ).model_dump_json()
            self._nearest_was_empty = False
            await self._client.publish(
                f"{self._base}/summary/nearest",
                nearest_payload,
                retain=True,
                topic_class="summary",
            )
        elif not self._nearest_was_empty:
            # First tick with no aircraft in range: clear the retained topic
            # once. Republishing b"" every tick made HA's MQTT integration
            # log "Erroneous JSON" once per publish interval, since it tries
            # to json-decode state_topic payloads before value_template runs.
            self._nearest_was_empty = True
            await self._client.publish(
                f"{self._base}/summary/nearest",
                b"",
                retain=True,
                topic_class="summary",
            )

        flags = count_by_flag if count_by_flag is not None else {}
        await self._client.publish(
            f"{self._base}/summary/count_by_flag",
            json.dumps(flags, separators=(",", ":")).encode("utf-8"),
            retain=True,
            topic_class="summary",
        )

        for flag, feed in (flag_feeds or {}).items():
            await self._client.publish(
                f"{self._base}/summary/by_flag/{flag}",
                feed.model_dump_json(),
                retain=True,
                topic_class="summary",
            )
        return True

    # ------------------------------------------------------------------
    # Per-receiver topics
    # ------------------------------------------------------------------

    async def publish_receiver_status(
        self, name: str, *, online: bool, unhealthy: bool = False
    ) -> None:
        """Publish ``airspace/receiver/<name>/status``: ``online`` |
        ``unhealthy`` | ``offline``. Retained — HA's connectivity
        binary_sensor reads from this directly via ``payload_on=online``.

        Published only when the status *changes* (deduped per receiver). The
        value flips rarely, so republishing it every poll is pure broker/SD-card
        churn; the retained topic already holds the current value for late
        subscribers."""
        if not online:
            status = "offline"
        elif unhealthy:
            status = "unhealthy"
        else:
            status = "online"
        if self._last_receiver_status.get(name) == status:
            return
        self._last_receiver_status[name] = status
        await self._client.publish(
            f"{self._base}/receiver/{name}/status",
            status.encode("utf-8"),
            retain=True,
            topic_class="status",
        )

    async def publish_receiver_stats(self, name: str, health: dict[str, object]) -> None:
        """Publish ``airspace/receiver/<name>/stats`` as JSON. HA's
        per-receiver count and message-rate sensors extract via
        ``value_template`` from this single topic."""
        payload = ReceiverStatsPayload.from_health(health).model_dump_json()
        await self._client.publish(
            f"{self._base}/receiver/{name}/stats",
            payload,
            retain=True,
            topic_class="status",
        )

    async def publish_receiver_location(self, name: str, location: ReceiverLocation) -> None:
        """Publish ``airspace/receiver/<name>/location`` as JSON. Published
        once at startup (and republished on reconnect via on_connect)."""
        payload = ReceiverLocationPayload.from_runtime(location).model_dump_json()
        await self._client.publish(
            f"{self._base}/receiver/{name}/location",
            payload,
            retain=True,
            topic_class="status",
        )


def _alert_info(state: AircraftState, photo: PhotoPayload | None) -> dict[str, Any]:
    """Compact summary of an alert's triggering track, for the per-rule
    ``info`` topic that the HA binary_sensor exposes as attributes. ``photo``
    maps to ``entity_picture`` so a picture/glance card renders the thumbnail.

    Self-sufficient for **both** bands. A drone-triggered rule
    (``drone_conflict``, ``drone_nearby``) additionally carries the Remote-ID
    fields, so a consumer never has to cross-reference
    ``sensor.airspace_nearest_drone`` to describe what fired — which silently
    described the *wrong* drone whenever a second, non-nearest drone was the
    one that actually tripped the rule.

    Aircraft keys stay present on a drone track rather than being branched
    away: they are all ``None`` there (a drone observation carries ``hex=None``,
    so ``country_for`` yields ``None`` rather than a bogus registry hit), and a
    stable key set means a consumer template never has to guard for a *missing*
    attribute, only a null one. ``is_drone`` is the discriminator.
    """
    canonical = state.canonical
    drone = canonical.drone
    info: dict[str, Any] = {
        "is_drone": drone is not None,
        "flight": canonical.flight,
        "hex": state.hex,
        "track_id": state.track_id,
        "registration": canonical.registration,
        "aircraft_type": canonical.aircraft_type,
        "country": country_for(state.hex),
        "country_flag": flag_for(state.hex),
        "squawk": canonical.squawk,
        "alt_baro_ft": canonical.alt_baro_ft,
        "vertical_rate_fpm": canonical.vertical_rate_fpm,
        "distance_to": dict(state.distance_to),
        "bearing_to": dict(state.bearing_to),
        "predicted_closest_approach_nm": state.predicted_closest_approach_nm,
        "predicted_eta_to_home_s": state.predicted_eta_to_home_s,
        "flags": sorted(state.flags),
        # Reference-DB join for aircraft; FAA UAS make/model for drones. The
        # documented attribute set has always listed this; it was missing.
        "db_metadata": dict(state.db_metadata),
    }
    if drone is not None:
        # Mirrors the RID-only half of DronePayload.from_state. Kept in sync by
        # test_alert_info_covers_every_rid_field_dronepayload_publishes.
        info.update(
            {
                "id_type": drone.id_type,
                "ua_type": drone.ua_type,
                "self_id": drone.self_id,
                "agl_ft": drone.agl_ft,
                "alt_geom_ft": canonical.alt_geom_ft,
                "ground_speed_kt": canonical.ground_speed_kt,
                "track_deg": canonical.track_deg,
                "rid_source": drone.rid_source,
                "operator_lat": drone.operator_lat,
                "operator_lon": drone.operator_lon,
                # Check this before treating operator_lat/lon as a live fix:
                # `takeoff` is the drone's own launch point.
                "operator_location_type": drone.operator_location_type,
                "operator_id": drone.operator_id,
                "operator_alt_takeoff_ft": drone.operator_alt_takeoff_ft,
            }
        )
    if photo is not None:
        info["entity_picture"] = photo.thumbnail_url
        info["photographer"] = photo.photographer
        info["photo_link"] = photo.link
    return info


__all__ = ["Publisher"]
