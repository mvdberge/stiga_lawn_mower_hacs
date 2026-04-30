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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import StigaAPI, StigaApiError, StigaAuthError
from .const import DOMAIN, UPDATE_INTERVAL
from .mqtt_client import StigaMQTT

_LOGGER = logging.getLogger(__name__)

_ISSUE_CONNECTION = "connection_error"
MAX_CONSECUTIVE_FAILURES = 3

_UPDATE_TIMEOUT = UPDATE_INTERVAL - 5

# Static metadata (model name, garden perimeter) only changes when the user
# touches the STIGA.GO app. We refresh it every 6 hours instead of once per
# integration setup so updates eventually propagate without forcing a reload.
META_REFRESH_INTERVAL = timedelta(hours=6)


class StigaDataUpdateCoordinator(DataUpdateCoordinator[dict]):
    """
    Central coordinator for all STIGA devices.

    data structure after update:
    {
        "devices":  [ { "attributes": { "uuid": ..., "name": ..., ... } }, ... ],
        "statuses": { "<uuid>": { "mowing_mode": ..., "battery_level": ...,
                                  # MQTT-only fields when available:
                                  "current_zone": ..., "zone_completed_pct": ...,
                                  "rssi": ..., "info_code": ..., ... }, ... },
        "meta":     { "<uuid>": { "model_name": "A 15v",
                                  "garden_area_m2": 656, ... }, ... },
        "mqtt_connected": bool,
        "live_position": { "<uuid>": {"lat_offset_m": ..., ...} },
        "live_settings": { "<uuid>": {...} },
        "live_schedule": { "<uuid>": {...} },
        "live_base_status": { "<base_uuid>": {...} },
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
        self._devices: list[dict] = []
        self._meta: dict[str, dict] = {}
        self._meta_next_refresh: datetime | None = None

        # Latest MQTT pushes, keyed by MAC address. Status frames feed into
        # the merged per-device `statuses[uuid]` dict; the others stay in
        # their own buckets so the entity layer (Phase 4 onwards) can pick
        # them up without reaching back into raw protobuf.
        self._live_status: dict[str, dict[str, Any]] = {}
        self._live_position: dict[str, dict[str, Any]] = {}
        self._live_settings: dict[str, dict[str, Any]] = {}
        self._live_schedule: dict[str, dict[str, Any]] = {}
        self._live_base_status: dict[str, dict[str, Any]] = {}
        self._mqtt_connected: bool = False

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    # -------------------------------------------------------------- MQTT wiring

    def attach_mqtt(self, mqtt: StigaMQTT) -> None:
        """Register MQTT push handlers; call once before starting the client."""
        self.mqtt = mqtt
        mqtt.set_handlers(
            on_status=self._on_mqtt_status,
            on_position=self._on_mqtt_position,
            on_settings=self._on_mqtt_settings,
            on_schedule=self._on_mqtt_schedule,
            on_base_status=self._on_mqtt_base_status,
            on_connection_change=self._on_mqtt_connected,
        )

    def _on_mqtt_status(self, mac: str, data: dict[str, Any]) -> None:
        if not data:
            _LOGGER.debug("MQTT STATUS frame for %s decoded to empty dict (protobuf issue?)", mac)
        else:
            _LOGGER.debug("MQTT STATUS for %s: %s", mac, list(data.keys()))
        self._live_status[mac] = data
        self._publish_update()

    def _on_mqtt_position(self, mac: str, data: dict[str, Any]) -> None:
        self._live_position[mac] = data
        self._publish_update()

    def _on_mqtt_settings(self, mac: str, data: dict[str, Any]) -> None:
        self._live_settings[mac] = data
        self._publish_update()

    def _on_mqtt_schedule(self, mac: str, data: dict[str, Any]) -> None:
        self._live_schedule[mac] = data
        self._publish_update()

    def _on_mqtt_base_status(self, mac: str, data: dict[str, Any]) -> None:
        self._live_base_status[mac] = data
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

    def _build_data(self, *, rest_statuses: dict[str, dict] | None = None) -> dict:
        """Assemble the coordinator's `data` dict from REST + live state.

        Called both at the end of the regular REST poll (with fresh
        ``rest_statuses``) and from MQTT push handlers (which reuse the
        statuses from the previous publish). The merged ``statuses`` dict
        is what every entity reads from today; the ``live_*`` buckets
        carry MQTT-only fields for new entities in later phases.
        """
        if rest_statuses is None:
            rest_statuses = (self.data or {}).get("statuses", {}) or {}

        statuses: dict[str, dict] = {}
        for device in self._devices:
            uuid = _device_uuid(device)
            if not uuid:
                continue
            mac = (device.get("attributes") or {}).get("mac_address")
            base = dict(rest_statuses.get(uuid) or {})
            live = self._live_status.get(mac, {}) if mac else {}
            statuses[uuid] = _merge_live_into_status(base, live)

        return {
            "devices": self._devices,
            "statuses": statuses,
            "meta": self._meta,
            "mqtt_connected": self._mqtt_connected,
            "live_position": dict(self._live_position),
            "live_settings": dict(self._live_settings),
            "live_schedule": dict(self._live_schedule),
            "live_base_status": dict(self._live_base_status),
        }

    async def _async_setup(self) -> None:
        """Fetch the initial device list and the first batch of static metadata."""
        self._devices = await self.api.get_devices()
        if not self._devices:
            raise UpdateFailed("No STIGA devices found for this account.")
        await self._refresh_meta()
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
            entry: dict = {}
            extended = await self.api.get_device_extended(uuid)
            entry.update(_extract_model_name(extended))
            base_uuid = (device.get("attributes") or {}).get("base_uuid")
            if base_uuid:
                perimeter = await self.api.get_perimeter(uuid, base_uuid)
                entry.update(_extract_perimeter(perimeter))
            if entry:
                self._meta[uuid] = entry

    async def _async_update_data(self) -> dict:
        """Refresh devices and status for all known devices."""
        try:
            async with asyncio.timeout(_UPDATE_TIMEOUT):
                # Refresh device list so newly added/removed robots are picked up
                # without requiring a Home Assistant restart.
                try:
                    devices = await self.api.get_devices()
                except StigaApiError as err:
                    _LOGGER.debug("Device list refresh failed, using cached: %s", err)
                else:
                    if devices:
                        self._devices = devices

                statuses: dict[str, dict] = {}
                previous = (self.data or {}).get("statuses", {})
                for device in self._devices:
                    uuid = _device_uuid(device)
                    if not uuid:
                        continue
                    try:
                        status = await self.api.get_device_status(uuid)
                    except StigaApiError as err:
                        _LOGGER.debug("Status fetch for %s failed: %s", uuid, err)
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

            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_CONNECTION)
                _LOGGER.info(
                    "STIGA cloud connection restored after %d failures.",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0

            return self._build_data(rest_statuses=statuses)

        except StigaAuthError as err:
            raise ConfigEntryAuthFailed from err

        except StigaApiError as err:
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
            raise UpdateFailed(f"STIGA API error: {err}") from err


def _device_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")


# MQTT-only fields that the entity layer pre-Phase-4 doesn't render but
# which we surface as state attributes via `extra_*` mapping. Keeping the
# list here makes it trivial to extend without touching merge plumbing.
_MQTT_PASSTHROUGH_FIELDS = (
    "current_zone",
    "zone_completed_pct",
    "garden_completed_pct",
    "satellites",
    "gps_quality",
    "rtk_fix_type",
    "rssi",
    "rsrp",
    "rsrq",
    "battery_voltage",
    "battery_current",
    "battery_temp_c",
    "info_label",
    "info_sensor",
    "operable",
)


def _merge_live_into_status(base: dict, live: dict) -> dict:
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
    # status_type when REST battery data is absent (e.g. while paused).
    if out.get("battery_charging") is None and status_type is not None:
        out["battery_charging"] = status_type == "CHARGING"

    if (info_code := live.get("info_code")) is not None:
        out["error_code"] = info_code
    # Any live frame proves the mower is online and emitting data.
    out["has_data"] = True

    for key in _MQTT_PASSTHROUGH_FIELDS:
        if key in live:
            out[key] = live[key]

    return out


def _enrich_status_from_device(status: dict, device: dict) -> None:
    """Merge sensor-relevant fields from /api/garage device attributes into status.

    The undocumented `/api/garage` endpoint returns attributes like
    `total_work_time` and `parsedSettings.cutting_height` that the documented
    `/api/garage/integration` does not. When those fields are present we
    surface them to the entity layer; otherwise the sensor goes unavailable.
    """
    attrs = device.get("attributes") or {}

    twt = attrs.get("total_work_time")
    if isinstance(twt, (int, float)):
        status["total_work_time"] = int(twt)

    settings = attrs.get("settings")
    if isinstance(settings, list) and settings:
        parsed = (settings[0] or {}).get("parsedSettings") or {}
        ch = parsed.get("cutting_height")
        if isinstance(ch, str) and ch.lower().endswith("mm"):
            with contextlib.suppress(ValueError):
                status["cutting_height_mm"] = int(ch[:-2])


def _extract_model_name(extended: dict) -> dict:
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


def _extract_perimeter(perimeter: dict) -> dict:
    """Flatten /perimeters response into the small set of fields we surface."""
    preview = ((perimeter.get("data") or {}).get("attributes") or {}).get("preview") or {}
    if not preview:
        return {}
    out: dict = {}
    if (m2 := preview.get("m2Area")) is not None:
        out["garden_area_m2"] = m2
    zones = preview.get("zones") or {}
    if (zn := zones.get("num")) is not None:
        out["zone_count"] = zn
    obstacles = preview.get("obstacles") or {}
    if (obn := obstacles.get("num")) is not None:
        out["obstacle_count"] = obn
    if (oba := obstacles.get("m2Area")) is not None:
        out["obstacle_area_m2"] = oba
    return out
