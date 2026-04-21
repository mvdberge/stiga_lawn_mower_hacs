"""DataUpdateCoordinator for STIGA robotic lawn mowers."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import StigaAPI, StigaApiError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

_ISSUE_CONNECTION = "connection_error"
# Number of consecutive failures before a persistent HA repair issue is created.
MAX_CONSECUTIVE_FAILURES = 3


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

    def __init__(self, hass: HomeAssistant, api: StigaAPI) -> None:
        self.api = api
        self._consecutive_failures = 0
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self) -> dict:
        """Fetch data from the STIGA API."""
        try:
            devices = await self.api.get_devices()
            statuses: dict[str, dict] = {}

            for device in devices:
                uuid = _device_uuid(device)
                if not uuid:
                    continue
                try:
                    statuses[uuid] = await self.api.get_device_status(uuid)
                except StigaApiError as err:
                    _LOGGER.warning("Could not fetch status for %s: %s", uuid, err)
                    statuses[uuid] = {}

            # Successful update – clear any outstanding repair issue.
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_CONNECTION)
                _LOGGER.info("STIGA cloud connection restored after %d failures.", self._consecutive_failures)
            self._consecutive_failures = 0

            return {"devices": devices, "statuses": statuses}

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
