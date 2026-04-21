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

# Hard cap on each full update cycle; must be less than UPDATE_INTERVAL.
# Prevents a slow/hanging API from blocking the event loop indefinitely.
_UPDATE_TIMEOUT = UPDATE_INTERVAL - 5


class StigaDataUpdateCoordinator(DataUpdateCoordinator):
    """
    Central coordinator for all STIGA devices.
    Fetches all devices and their statuses in a single pass.

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
        """Fetch the device list once at startup – devices rarely change.

        Called automatically by async_config_entry_first_refresh().
        Separating this from _async_update_data means every 30-second poll
        only fetches status endpoints, not the full device list.
        """
        self._devices = await self.api.get_devices()
        if not self._devices:
            raise UpdateFailed("No STIGA devices found for this account.")

    async def _async_update_data(self) -> dict:
        """Fetch current status for all known devices."""
        try:
            async with asyncio.timeout(_UPDATE_TIMEOUT):
                statuses: dict[str, dict] = {}

                previous = (self.data or {}).get("statuses", {})
                for device in self._devices:
                    uuid = _device_uuid(device)
                    if not uuid:
                        continue
                    try:
                        statuses[uuid] = await self.api.get_device_status(uuid)
                    except StigaApiError as err:
                        _LOGGER.warning("Could not fetch status for %s: %s", uuid, err)
                        # Keep last known data so sensors don't drop to unavailable
                        # on a transient error.
                        statuses[uuid] = previous.get(uuid, {})

            # Successful update – clear any outstanding repair issue.
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_CONNECTION)
                _LOGGER.info(
                    "STIGA cloud connection restored after %d failures.",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0

            return {"devices": self._devices, "statuses": statuses}

        except StigaAuthError as err:
            # Invalid credentials – stop retrying and prompt the user to re-authenticate.
            raise ConfigEntryAuthFailed from err

        except StigaApiError as err:
            self._consecutive_failures += 1
            _LOGGER.warning(
                "STIGA API update failed (attempt %d): %s",
                self._consecutive_failures,
                err,
            )
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
