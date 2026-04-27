"""DataUpdateCoordinator for STIGA robotic lawn mowers."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import StigaAPI, StigaApiError, StigaAuthError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

_ISSUE_CONNECTION = "connection_error"
MAX_CONSECUTIVE_FAILURES = 3

_UPDATE_TIMEOUT = UPDATE_INTERVAL - 5


class StigaDataUpdateCoordinator(DataUpdateCoordinator[dict]):
    """
    Central coordinator for all STIGA devices.

    data structure after update:
    {
        "devices": [ { "attributes": { "uuid": ..., "name": ..., ... } }, ... ],
        "statuses": {
            "<uuid>": {
                "mowing_mode":    str | int,
                "current_action": str | int,
                "battery_level":  int,
                ...
            },
            ...
        }
    }
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: StigaAPI) -> None:
        self.api = api
        self._consecutive_failures = 0
        self._devices: list[dict] = []
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_setup(self) -> None:
        """Fetch the initial device list."""
        self._devices = await self.api.get_devices()
        if not self._devices:
            raise UpdateFailed("No STIGA devices found for this account.")

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

            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_CONNECTION)
                _LOGGER.info(
                    "STIGA cloud connection restored after %d failures.",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0

            return {"devices": self._devices, "statuses": statuses}

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
