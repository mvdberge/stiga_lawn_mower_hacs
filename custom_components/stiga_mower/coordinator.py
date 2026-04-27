"""DataUpdateCoordinator for STIGA robotic lawn mowers."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import StigaAPI, StigaApiError, StigaAuthError
from .const import DOMAIN, UPDATE_INTERVAL

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
        "statuses": { "<uuid>": { "mowing_mode": ..., "battery_level": ..., ... }, ... },
        "meta":     { "<uuid>": { "model_name": "A 15v",
                                  "garden_area_m2": 656,
                                  "zone_count": 5,
                                  "obstacle_count": 8,
                                  "obstacle_area_m2": 69.2 }, ... }
    }
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: StigaAPI) -> None:
        self.api = api
        self._consecutive_failures = 0
        self._devices: list[dict] = []
        self._meta: dict[str, dict] = {}
        self._meta_next_refresh: datetime | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

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

            return {
                "devices":  self._devices,
                "statuses": statuses,
                "meta":     self._meta,
            }

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
            try:
                status["cutting_height_mm"] = int(ch[:-2])
            except ValueError:
                pass


def _extract_model_name(extended: dict) -> dict:
    """Pull friendly model name (`A 15v`) from /devices/{uuid} `included[]`."""
    for inc in (extended.get("included") or []):
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
