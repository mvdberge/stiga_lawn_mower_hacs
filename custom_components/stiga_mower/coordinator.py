"""DataUpdateCoordinator for STIGA robotic lawn mowers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import StigaAPI, StigaApiError, StigaAuthError
from .const import DOMAIN, UPDATE_INTERVAL, split_firmware_version
from .mqtt_client import StigaMQTT

_LOGGER = logging.getLogger(__name__)

_ISSUE_CONNECTION = "connection_error"
MAX_CONSECUTIVE_FAILURES = 3

_UPDATE_TIMEOUT = UPDATE_INTERVAL - 5

# How long cached REST data is considered fresh enough to keep entities
# available. After this window the cloud has been unreachable long enough
# that showing stale data is misleading.
_STALE_DATA_THRESHOLD = timedelta(minutes=10)

# Static metadata (model name, garden perimeter) only changes when the user
# touches the STIGA.GO app. We refresh it every 6 hours instead of once per
# integration setup so updates eventually propagate without forcing a reload.
META_REFRESH_INTERVAL = timedelta(hours=6)


class StigaDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Central coordinator for all STIGA devices.

    data structure after update:
    {
        "devices":  [ { "attributes": { "uuid": ..., "name": ..., ... } }, ... ],
        "statuses": { "<uuid>": { "mowing_mode": ..., "battery_level": ...,
                                  # MQTT-only fields when available:
                                  "current_zone": ..., "zone_completed_pct": ...,
                                  "rsrp": ..., "info_code": ..., ... }, ... },
        "meta":     { "<uuid>": { "model_name": "A 15v",
                                  "garden_area_m2": 656, ... }, ... },
        "mqtt_connected": bool,
        "live_settings": { "<mac>": {...} },
        "live_schedule": { "<mac>": {...} },
        "live_base_status": { "<base_mac>": {...} },
        "live_base_version": { "<base_mac>": {...} },
        "bases": [ { "uuid": ..., "mac_address": ..., "firmware_version": ..., ... }, ... ],
    }

    The coordinator is push-driven for MQTT frames (each frame triggers
    `async_set_updated_data` so entities update immediately) and pull-driven
    for REST data (every UPDATE_INTERVAL seconds for liveness + state that
    only the cloud knows: total_work_time, perimeter, model name).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: StigaAPI,
        mqtt: StigaMQTT | None = None,
    ) -> None:
        self.api = api
        self.mqtt = mqtt
        self._consecutive_failures = 0
        self._devices: list[dict[str, Any]] = []
        self._meta: dict[str, dict[str, Any]] = {}
        self._meta_next_refresh: datetime | None = None

        # Latest MQTT pushes, keyed by MAC address. Status frames feed into
        # the merged per-device `statuses[uuid]` dict; the others stay in
        # their own buckets so the entity layer (Phase 4 onwards) can pick
        # them up without reaching back into raw protobuf.
        self._live_status: dict[str, dict[str, Any]] = {}
        self._live_settings: dict[str, dict[str, Any]] = {}
        self._live_schedule: dict[str, dict[str, Any]] = {}
        self._live_base_status: dict[str, dict[str, Any]] = {}
        self._live_base_version: dict[str, dict[str, Any]] = {}
        # REST-side base-station snapshot (from /api/garage included[OwnBases]).
        self._bases: list[dict[str, Any]] = []
        self._mqtt_connected: bool = False

        # Timestamp of the last successful REST poll. Used to decide whether
        # cached data is still recent enough to keep entities available.
        self._last_rest_success: datetime | None = None

        # Per-device timestamp of the last time we saw valid telemetry
        # (a non-empty status that was not explicitly hasData=False). Lets
        # entities ride out brief `hasData:false` blips from the cloud without
        # flapping to "unavailable", while still going unavailable once the
        # mower has genuinely been reporting no data for _STALE_DATA_THRESHOLD.
        self._last_has_data: dict[str, datetime] = {}

        # Last `firmware_version` string we observed per device UUID. HA's
        # device registry only consumes `device_info` at entity-registration
        # time, so a firmware update done via STIGA.GO would otherwise stay
        # invisible until the integration is reloaded. We track the raw
        # string and push changes through `_sync_device_registry_firmware`.
        self._known_firmware: dict[str, str] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    # -------------------------------------------------------------- Public helpers

    @property
    def rest_data_fresh(self) -> bool:
        """True when REST data was fetched recently enough to be trustworthy.

        Returns False only after _STALE_DATA_THRESHOLD has elapsed without a
        successful poll — i.e. the cloud has been unreachable for an extended
        period and showing cached values would be misleading.
        """
        if self._last_rest_success is None:
            return False
        return dt_util.utcnow() - self._last_rest_success < _STALE_DATA_THRESHOLD

    def has_data_fresh(self, uuid: str) -> bool:
        """True when the device reported valid telemetry recently enough.

        Mirrors `rest_data_fresh` but per device and keyed on the STIGA
        `hasData` flag. A single `hasData:false` frame (which the cloud emits
        intermittently while the mower sleeps or between reports) therefore no
        longer flaps every entity to "unavailable"; only a sustained absence of
        valid data past _STALE_DATA_THRESHOLD does.
        """
        ts = self._last_has_data.get(uuid)
        if ts is None:
            return False
        return dt_util.utcnow() - ts < _STALE_DATA_THRESHOLD

    # -------------------------------------------------------------- MQTT wiring

    def attach_mqtt(self, mqtt: StigaMQTT) -> None:
        """Register MQTT push handlers; call once before starting the client."""
        self.mqtt = mqtt
        mqtt.set_handlers(
            on_status=self._on_mqtt_status,
            on_settings=self._on_mqtt_settings,
            on_schedule=self._on_mqtt_schedule,
            on_base_status=self._on_mqtt_base_status,
            on_base_version=self._on_mqtt_base_version,
            on_connection_change=self._on_mqtt_connected,
        )

    def _on_mqtt_status(self, mac: str, data: dict[str, Any]) -> None:
        if not data:
            _LOGGER.debug("MQTT STATUS frame for %s decoded to empty dict (protobuf issue?)", mac)
        else:
            _LOGGER.debug("MQTT STATUS for %s: %s", mac, list(data.keys()))
        # STIGA firmware alternates between full frames (battery + location +
        # network sub-messages present) and scoped partial frames (mowing-only).
        # Carry sticky telemetry forward so total_work_time, RSSI, battery
        # temperature etc. don't flicker to "unavailable" between full frames.
        prev = self._live_status.get(mac, {})
        self._live_status[mac] = _merge_sticky_live(prev, data) if data else prev
        self._publish_update()

    def build_settings_payload(self, mac: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Augment a settings update with the atomic sibling fields.

        cmd_settings_update is more strictly atomic than the protobuf layout
        suggests: omitting any of fields {1 (rain), 2 (sleep_mode), 4 (cutting),
        9 (zone_cutting_height_uniform), 11 (firmware-internal varint)} from an
        outbound payload resets that field firmware-side to the proto3 default.
        Empirically this fires even when the write targets a completely
        unrelated submsg (e.g. push_notifications field 14).

        The STIGA.GO app sends all five every time (capture 2026-06-02). We
        mirror that by backfilling each key from live_settings whenever it is
        known and not already set explicitly by the caller. unknown_11 is
        opaque — we never invent a value, we only echo what the firmware sent
        us; if the robot has not yet produced a SETTINGS frame the field stays
        absent, which matches the firmware default and is harmless.
        """
        live = self._live_settings.get(mac, {})
        payload = dict(changes)
        for key in (
            "rain_sensor_enabled",
            "rain_sensor_delay_h",
            "sleep_mode",
            "zone_cutting_height_enabled",
            "cutting_height_mm",
            "zone_cutting_height_uniform",
            "unknown_11",
        ):
            if key not in payload and (cur := live.get(key)) is not None:
                payload[key] = cur
        return payload

    def apply_live_settings(self, mac: str, settings: dict[str, Any]) -> None:
        """Optimistically merge settings into live_settings and notify listeners.

        Called by write-entities immediately after publishing a MQTT command so
        the UI reflects the new state without waiting for the firmware response.
        This is necessary because the STIGA firmware omits proto3-default fields
        (bool False = 0, delay index 0 = 4 h) from SETTINGS frames, which the
        coordinator's merge-on-receive cannot detect: a field transitioning to
        its default simply disappears from the response, leaving live_settings
        stale.
        """
        self._live_settings.setdefault(mac, {}).update(settings)
        self._publish_update()

    def apply_live_schedule(self, mac: str, schedule: dict[str, Any]) -> None:
        """Optimistically merge schedule into live_schedule and notify listeners.

        Same rationale as apply_live_settings: schedule enabled=False is the
        proto3 default and gets omitted from the firmware's SCHEDULING_SETTINGS
        response, so the coordinator merge cannot detect a disable transition.
        """
        self._live_schedule.setdefault(mac, {}).update(schedule)
        self._publish_update()

    def _on_mqtt_settings(self, mac: str, data: dict[str, Any]) -> None:
        # SETTINGS frames are sparse full snapshots: every non-default field is
        # present, fields at their proto3 default are omitted. Absence of a
        # submsg in a non-empty frame genuinely means "all its fields at
        # default" (verified empirically via capture/inject_settings.py on
        # 2026-05-18 — see capture/bug2_capture.jsonl). We still merge instead
        # of replace so an empty decode (parse error) does not wipe state, and
        # so any future partial sub-frame would degrade gracefully.
        if data:
            self._live_settings[mac] = {**self._live_settings.get(mac, {}), **data}
        else:
            self._live_settings.setdefault(mac, {})
        self._publish_update()

    def _on_mqtt_schedule(self, mac: str, data: dict[str, Any]) -> None:
        # Same merge rule as _on_mqtt_settings: cmd_schedule_set_enabled emits a
        # partial frame containing only the ``enabled`` flag, which would
        # otherwise wipe the stored ``days`` blob.
        if data:
            self._live_schedule[mac] = {**self._live_schedule.get(mac, {}), **data}
        else:
            self._live_schedule.setdefault(mac, {})
        self._publish_update()

    def _on_mqtt_base_status(self, mac: str, data: dict[str, Any]) -> None:
        # BASE STATUS frames carry top-level status (type/flag/led) plus
        # location/network sub-messages. Merge so a status-only frame does
        # not wipe previously seen location/network values.
        if data:
            self._live_base_status[mac] = {**self._live_base_status.get(mac, {}), **data}
        else:
            self._live_base_status.setdefault(mac, {})
        self._publish_update()

    def _on_mqtt_base_version(self, mac: str, data: dict[str, Any]) -> None:
        self._live_base_version[mac] = data
        self._publish_update()

    def _on_mqtt_connected(self, connected: bool) -> None:
        self._mqtt_connected = connected
        self._publish_update()

    def _publish_update(self) -> None:
        """Push the merged state to entity listeners.

        Skipped before the first regular refresh so we never publish a
        half-built payload (entities subscribe after `_async_setup` returns).
        """
        if self.data is None:
            return
        self.async_set_updated_data(self._build_data())

    # -------------------------------------------------------------- Build / merge

    def _build_data(
        self, *, rest_statuses: dict[str, dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Assemble the coordinator's `data` dict from REST + live state.

        Called both at the end of the regular REST poll (with fresh
        ``rest_statuses``) and from MQTT push handlers (which reuse the
        statuses from the previous publish). The merged ``statuses`` dict
        is what every entity reads from today; the ``live_*`` buckets
        carry MQTT-only fields for new entities in later phases.
        """
        if rest_statuses is None:
            rest_statuses = (self.data or {}).get("statuses", {}) or {}

        statuses: dict[str, dict[str, Any]] = {}
        for device in self._devices:
            uuid = _device_uuid(device)
            if not uuid:
                continue
            mac = (device.get("attributes") or {}).get("mac_address")
            base = dict(rest_statuses.get(uuid) or {})
            live = self._live_status.get(mac, {}) if mac else {}
            merged = _merge_live_into_status(base, live)
            statuses[uuid] = merged
            # Record the moment we last saw valid telemetry so `has_data_fresh`
            # can debounce transient `hasData:false` blips (see its docstring).
            if merged and merged.get("has_data") is not False:
                self._last_has_data[uuid] = dt_util.utcnow()

        # Return shallow copies of the live buckets so background refreshers
        # (_refresh_meta, get_devices) mutating self._meta / self._devices in
        # place do not change a snapshot already handed to consumers.
        return {
            "devices": list(self._devices),
            "statuses": statuses,
            "meta": dict(self._meta),
            "mqtt_connected": self._mqtt_connected,
            "live_settings": dict(self._live_settings),
            "live_schedule": dict(self._live_schedule),
            "live_base_status": dict(self._live_base_status),
            "live_base_version": dict(self._live_base_version),
            "bases": list(self._bases),
        }

    async def _async_setup(self) -> None:
        """Fetch the initial device list and the first batch of static metadata."""
        self._devices = await self.api.get_devices()
        if not self._devices:
            raise UpdateFailed("No STIGA devices found for this account.")
        await self._refresh_meta()
        self._bases = await self.api.get_bases()
        self._meta_next_refresh = dt_util.utcnow() + META_REFRESH_INTERVAL

    async def _refresh_meta(self) -> None:
        """Best-effort fetch of model name + perimeter for each device.

        Both endpoints are undocumented. Failure is non-fatal: the meta dict
        simply won't include the missing keys and the corresponding sensors
        stay unavailable.
        """
        for device in self._devices:
            uuid = _device_uuid(device)
            if not uuid:
                continue
            # Best-effort per device: a malformed response for one mower must
            # never abort setup or the periodic meta refresh for the others.
            try:
                entry: dict[str, Any] = {}
                extended = await self.api.get_device_extended(uuid)
                entry.update(_extract_model_name(extended))
                base_uuid = (device.get("attributes") or {}).get("base_uuid")
                if base_uuid:
                    perimeter = await self.api.get_perimeter(uuid, base_uuid)
                    entry.update(_extract_perimeter(perimeter))
                if entry:
                    self._meta[uuid] = entry
            except Exception as err:
                # Meta is non-critical: never let one device's malformed response
                # abort setup or the periodic refresh for the others.
                _LOGGER.debug("Meta refresh for %s failed (non-fatal): %s", uuid, err)

    def _sync_device_registry_firmware(self) -> None:
        """Push firmware_version changes from REST into the device registry.

        HA reads each entity's `device_info` exactly once (at registration);
        the `sw_version`/`hw_version` derived from `attributes.firmware_version`
        therefore never gets refreshed when the user flashes a new firmware
        via STIGA.GO. The REST poll already carries the new string in every
        `get_devices()` response — we just need to forward it to the device
        registry whenever it actually changes.
        """
        device_reg = dr.async_get(self.hass)
        for device in self._devices:
            uuid = _device_uuid(device)
            if not uuid:
                continue
            raw = (device.get("attributes") or {}).get("firmware_version")
            if not raw:
                continue
            if self._known_firmware.get(uuid) == raw:
                continue
            self._known_firmware[uuid] = raw
            entry = device_reg.async_get_device(identifiers={(DOMAIN, uuid)})
            # No registry entry yet means entities haven't been added yet —
            # their initial `device_info` will populate sw_version on first
            # registration, so there is nothing to update here.
            if entry is None:
                continue
            hw, fw, _build = split_firmware_version(raw)
            kwargs: dict[str, Any] = {}
            if fw:
                kwargs["sw_version"] = fw
            if hw and hw != fw:
                kwargs["hw_version"] = hw
            if kwargs:
                device_reg.async_update_device(entry.id, **kwargs)

    async def _refresh_bases(self) -> None:
        """Best-effort refresh of the base-station list from /api/garage.

        Bases are static-ish (firmware version, broker_id) so we fetch them
        on the same schedule as meta. Failure is non-fatal: keep the
        cached list rather than wiping it on a transient cloud hiccup.
        """
        try:
            bases = await self.api.get_bases()
        except StigaApiError as err:
            _LOGGER.debug("Base list refresh failed, keeping cached: %s", err)
            return
        if bases:
            self._bases = bases

    async def _async_update_data(self) -> dict[str, Any]:
        """Refresh devices and status for all known devices.

        Transient REST failures (timeouts, 5xx responses) return the last-known
        data so entities stay available during brief cloud outages. A
        persistent failure (no successful poll for _STALE_DATA_THRESHOLD) is
        surfaced via the issue registry and, if there is no cached data at all,
        raises UpdateFailed so HA marks the entry as broken.
        """
        try:
            async with asyncio.timeout(_UPDATE_TIMEOUT):
                last_error: Exception | None = None

                # Refresh device list so newly added/removed robots are picked up
                # without requiring a Home Assistant restart.
                try:
                    devices = await self.api.get_devices()
                except StigaApiError as err:
                    _LOGGER.debug("Device list refresh failed, using cached: %s", err)
                    last_error = err
                else:
                    if devices:
                        self._devices = devices
                        self._sync_device_registry_firmware()

                statuses: dict[str, dict[str, Any]] = {}
                previous = (self.data or {}).get("statuses", {})
                status_success = False
                status_attempted = False
                for device in self._devices:
                    uuid = _device_uuid(device)
                    if not uuid:
                        continue
                    status_attempted = True
                    try:
                        status = await self.api.get_device_status(uuid)
                    except StigaApiError as err:
                        _LOGGER.debug("Status fetch for %s failed: %s", uuid, err)
                        last_error = err
                        status = previous.get(uuid, {})
                    else:
                        if status:
                            status_success = True
                        else:
                            # HTTP 200 but an unrecognised/degraded body parsed
                            # to {} — the cloud emits these during instability.
                            # Treat it like a failed fetch: keep the last good
                            # snapshot instead of wiping it (which would flap
                            # every entity to "unavailable" for one cycle).
                            _LOGGER.debug(
                                "Status fetch for %s returned an empty payload, keeping cached",
                                uuid,
                            )
                            last_error = last_error or StigaApiError("empty status payload")
                            status = previous.get(uuid, {})
                    _enrich_status_from_device(status, device)
                    statuses[uuid] = status

            # Schedule a meta refresh every META_REFRESH_INTERVAL so changes
            # the user makes in the STIGA.GO app (e.g. re-drawing the
            # perimeter, renaming the mower) propagate without an integration
            # reload. Fire-and-forget so a slow `/perimeters` or `/devices/{uuid}`
            # call cannot trip the regular polling cycle. The next regular
            # update will publish the refreshed meta to listeners.
            now = dt_util.utcnow()
            if self._meta_next_refresh is None or now >= self._meta_next_refresh:
                self._meta_next_refresh = now + META_REFRESH_INTERVAL
                self.hass.async_create_task(self._refresh_meta())
                self.hass.async_create_task(self._refresh_bases())

            # The poll is only "successful" if at least one device's status
            # was fetched fresh. Without this guard, silently-swallowed API
            # errors would bump `_last_rest_success` every cycle and the
            # `_STALE_DATA_THRESHOLD` grace would never elapse — entities
            # would show frozen stale data indefinitely instead of going
            # unavailable after 10 minutes of cloud failure.
            if status_attempted and not status_success:
                return self._handle_poll_failure(
                    last_error or StigaApiError("all STIGA REST status calls failed silently")
                )

            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_CONNECTION)
                _LOGGER.info(
                    "STIGA cloud connection restored after %d failures.",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
            self._last_rest_success = dt_util.utcnow()

            return self._build_data(rest_statuses=statuses)

        except StigaAuthError as err:
            raise ConfigEntryAuthFailed from err

        except (StigaApiError, TimeoutError) as err:
            return self._handle_poll_failure(err)

    def _handle_poll_failure(self, err: Exception) -> dict[str, Any]:
        """Account a failed REST poll: bump failure counter, log, raise/keep.

        The ``_last_rest_success`` timestamp is intentionally *not* touched
        here — that is what lets `rest_data_fresh` eventually flip to False
        after `_STALE_DATA_THRESHOLD` of continuous failure, so entities can
        finally go unavailable instead of showing frozen stale data forever.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures == MAX_CONSECUTIVE_FAILURES:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                _ISSUE_CONNECTION,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=_ISSUE_CONNECTION,
                translation_placeholders={
                    "failures": str(self._consecutive_failures),
                    "error": str(err),
                },
            )
        # If we have no data at all (first-run failure), we must raise so
        # Home Assistant knows the entry is not usable yet.
        if self.data is None:
            raise UpdateFailed(f"STIGA API error: {err}") from err

        # Otherwise keep the previous data so entities stay visible during
        # transient cloud outages. Log at warning once per failure streak.
        if self._consecutive_failures == 1:
            _LOGGER.warning(
                "STIGA REST poll failed (%s) — keeping last known state. "
                "Entities will go unavailable after %s without a successful poll.",
                err,
                _STALE_DATA_THRESHOLD,
            )
        return self.data


def _device_uuid(device: dict[str, Any]) -> str:
    return str((device.get("attributes") or {}).get("uuid", ""))


# MQTT-only fields that the entity layer pre-Phase-4 doesn't render but
# which we surface as state attributes via `extra_*` mapping. Keeping the
# list here makes it trivial to extend without touching merge plumbing.
_MQTT_PASSTHROUGH_FIELDS = (
    "current_zone",
    "zone_completed_pct",
    "garden_completed_pct",
    "satellites",
    "rtk_fix_type",
    "rsrp",
    "rsrq",
    "battery_voltage",
    "battery_current",
    "battery_temp_c",
    "battery_remaining",
    "info_label",
    "info_sensor",
    "operable",
)


# Telemetry fields that the firmware emits only in *full* STATUS frames
# (battery sub-message field 17, location field 19, network field 20).
# Partial frames — emitted when the app polls a scoped sub-status — would
# otherwise wipe these values and force the entity to flicker between the
# live MQTT reading and the (often-stale) REST fallback.  Keep the last
# known value when the new frame doesn't carry the field.
#
# Status fields that *must* clear (info_code, info_label, info_sensor,
# docking) deliberately stay out of this set: when the robot exits an
# error state the next frame omits field 10, and we want that absence
# to clear the error rather than persist forever.
_STICKY_LIVE_FIELDS = frozenset(
    {
        "battery_capacity_mah",
        "battery_level",
        "battery_temp_c",
        "battery_current",
        "battery_voltage",
        "battery_remaining",
        "satellites",
        "rtk_fix_type",
        "network_kind",
        "network_type",
        "network_band",
        "rsrp",
        "rsrq",
    }
)


def _merge_sticky_live(prev: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Merge a fresh STATUS frame onto the previous one.

    Fields named in ``_STICKY_LIVE_FIELDS`` carry over from ``prev`` when
    the new frame does not include them; everything else takes its value
    from the new frame (or is dropped if the new frame omits it).
    """
    merged = dict(new)
    for key in _STICKY_LIVE_FIELDS:
        if key not in merged and key in prev:
            merged[key] = prev[key]
    return merged


def _merge_live_into_status(base: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Layer an MQTT status frame on top of the REST status dict.

    The entity layer reads from ``current_action``, ``mowing_mode``,
    ``is_docked``, ``error_code``, ``battery_level`` and ``has_data``.
    MQTT speaks ``status_type``, ``docking``, ``info_code`` etc.; this
    function translates the live frame into the REST schema so neither
    lawn_mower.py nor sensor.py needs to know about MQTT.
    """
    out = dict(base)
    if not live:
        return out

    # status_type strings (DOCKED, MOWING, GOING_HOME, …) intentionally
    # match the REST currentAction values matthewgream reverse-engineered
    # from the same protobuf, so the existing _CURRENT_ACTION map in
    # lawn_mower.py covers them without translation.
    status_type = live.get("status_type")
    if status_type is not None:
        out["current_action"] = status_type

    if (battery_level := live.get("battery_level")) is not None:
        out["battery_level"] = battery_level

    # Field 13 (docking bool) is only present in STATUS frames when the robot
    # is actively docking/docked.  When absent, fall back to status_type so
    # is_docked is never left as None while MQTT is live.
    docking = live.get("docking")
    if docking is not None:
        out["is_docked"] = docking
    elif status_type is not None:
        out["is_docked"] = status_type in ("DOCKED", "CHARGING")

    # The STATUS frame has no dedicated charging boolean; derive it from
    # status_type. The REST `battery.charging` flag lags reality (the cloud
    # may still report charging:true while the mower is already MOWING), so
    # the live MQTT status_type is the source of truth — same policy as
    # current_action / is_docked above.
    if status_type is not None:
        out["battery_charging"] = status_type == "CHARGING"

    if (info_code := live.get("info_code")) is not None:
        out["error_code"] = info_code
    elif live:
        # MQTT STATUS frames omit field 10 when no fault is active (proto3
        # default omission). The cloud's REST `errorCode` may continue to
        # report the last-seen fault long after it cleared (same stale-cache
        # behaviour as `battery_charging` above), so MQTT silence must win.
        out["error_code"] = None
    # Any live frame proves the mower is online and emitting data.
    out["has_data"] = True

    for key in _MQTT_PASSTHROUGH_FIELDS:
        if key in live:
            out[key] = live[key]

    return out


def _enrich_status_from_device(status: dict[str, Any], device: dict[str, Any]) -> None:
    """Merge sensor-relevant fields from /api/garage device attributes into status.

    The undocumented `/api/garage` endpoint returns attributes like
    `parsedSettings.cutting_height` and `settings[0].docking_version` that
    the documented `/api/garage/integration` does not. When those fields
    are present we surface them to the entity layer; otherwise the sensor
    goes unavailable.
    """
    attrs = device.get("attributes") or {}

    settings = attrs.get("settings")
    if isinstance(settings, list) and settings:
        first = settings[0] or {}
        parsed = first.get("parsedSettings") or {}
        ch = parsed.get("cutting_height")
        if isinstance(ch, str) and ch.lower().endswith("mm"):
            with contextlib.suppress(ValueError):
                status["cutting_height_mm"] = int(ch[:-2])
        # Docking-station firmware lives next to `parsedSettings`, not on the
        # robot itself — surface it as a separate sensor so it does not get
        # conflated with the robot's `firmware_version`.
        dv = first.get("docking_version")
        if isinstance(dv, str) and dv:
            status["dock_firmware"] = dv


def _extract_model_name(extended: dict[str, Any]) -> dict[str, Any]:
    """Pull friendly model name (`A 15v`) from /devices/{uuid} `included[]`."""
    for inc in extended.get("included") or []:
        if inc.get("type") != "DeviceDetails":
            continue
        items = ((inc.get("attributes") or {}).get("soap_info") or {}).get("item")
        if isinstance(items, list) and items:
            name = (items[0] or {}).get("Name")
            if isinstance(name, str) and name:
                return {"model_name": name}
    return {}


def _extract_perimeter(perimeter: dict[str, Any]) -> dict[str, Any]:
    """Flatten /perimeters response into the small set of fields we surface."""
    preview = ((perimeter.get("data") or {}).get("attributes") or {}).get("preview") or {}
    if not preview:
        return {}
    out: dict[str, Any] = {}
    if (m2 := preview.get("m2Area")) is not None:
        out["garden_area_m2"] = m2
    zones = preview.get("zones") or {}
    if (zn := zones.get("num")) is not None:
        out["zone_count"] = zn
    elements = zones.get("elements")
    if isinstance(elements, list) and elements:
        out["zone_elements"] = [
            {
                "id": e["id"],
                "area_m2": round(e["m2Area"], 2),
                "num_points": e["numPoints"],
            }
            for e in elements
            if isinstance(e, dict)
            and "id" in e
            and isinstance(e.get("m2Area"), (int, float))
            and isinstance(e.get("numPoints"), int)
        ]
    obstacles = preview.get("obstacles") or {}
    if (obn := obstacles.get("num")) is not None:
        out["obstacle_count"] = obn
    if (oba := obstacles.get("m2Area")) is not None:
        out["obstacle_area_m2"] = oba
    return out
