"""DataUpdateCoordinator für STIGA Mäh-Roboter."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import StigaAPI, StigaApiError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class StigaDataUpdateCoordinator(DataUpdateCoordinator):
    """
    Zentraler Koordinator für alle STIGA-Geräte.
    Ruft alle Geräte + deren Status in einem Zug ab.

    data-Struktur nach Update:
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
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self) -> dict:
        """Daten von der STIGA API abrufen."""
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
                    _LOGGER.warning("Status für %s nicht abrufbar: %s", uuid, err)
                    statuses[uuid] = {}

            return {"devices": devices, "statuses": statuses}

        except StigaApiError as err:
            raise UpdateFailed(f"STIGA API Fehler: {err}") from err


def _device_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
