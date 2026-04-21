"""STIGA LawnMower Entity für Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    ATTR_SERIAL_NUMBER,
    ATTR_PRODUCT_CODE,
    ATTR_DEVICE_TYPE,
    ATTR_MOWING_MODE_RAW,
    ATTR_BATTERY_VOLTAGE,
    ATTR_BATTERY_CAPACITY,
    ATTR_BATTERY_REMAINING,
    ATTR_BATTERY_CYCLES,
    ATTR_BATTERY_POWER,
    ATTR_BATTERY_HEALTH,
    ATTR_BATTERY_TIME_LEFT,
    ATTR_BATTERY_CURRENT,
    ATTR_ERROR_CODE,
)
from .coordinator import StigaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Zustandsmapping: STIGA-Modus → LawnMowerActivity
# Tatsächliche API-Werte: Strings bei vista_robot, Integers bei älteren Modellen
MOWING_MODE_TO_ACTIVITY: dict[Any, LawnMowerActivity] = {
    # String-Codes (vista_robot / neue Modelle)
    "WORKING":      LawnMowerActivity.MOWING,
    "BORDER":       LawnMowerActivity.MOWING,
    "MANUAL":       LawnMowerActivity.MOWING,
    "GOING_HOME":   LawnMowerActivity.PAUSED,   # kein RETURNING in HA, nächster passender Zustand
    "PAUSE":        LawnMowerActivity.PAUSED,
    "IDLE":         LawnMowerActivity.DOCKED,
    "CHARGING":     LawnMowerActivity.DOCKED,
    "SCHEDULED":    LawnMowerActivity.DOCKED,
    "SLEEPING":     LawnMowerActivity.DOCKED,
    "UPDATING":     LawnMowerActivity.DOCKED,
    "ERROR":        LawnMowerActivity.ERROR,
    "LOCKED":       LawnMowerActivity.ERROR,
    # Integer-Codes (ältere autonomous_robot Modelle)
    1:  LawnMowerActivity.MOWING,
    7:  LawnMowerActivity.MOWING,
    2:  LawnMowerActivity.PAUSED,
    3:  LawnMowerActivity.PAUSED,
    4:  LawnMowerActivity.ERROR,
    6:  LawnMowerActivity.ERROR,
    5:  LawnMowerActivity.DOCKED,
    8:  LawnMowerActivity.DOCKED,
    0:  LawnMowerActivity.DOCKED,
}

# Für den Benutzer lesbare Statusbeschreibung (als Attribut)
MOWING_MODE_LABELS: dict[Any, str] = {
    "WORKING":    "Mäht",
    "BORDER":     "Randfahrt",
    "MANUAL":     "Manuell",
    "GOING_HOME": "Kehrt zur Station zurück",
    "PAUSE":      "Pausiert",
    "IDLE":       "Bereit",
    "CHARGING":   "Lädt",
    "SCHEDULED":  "Geplant",
    "SLEEPING":   "Schläft",
    "UPDATING":   "Update",
    "ERROR":      "Fehler",
    "LOCKED":     "Gesperrt",
    1: "Mäht",           2: "Kehrt zurück",
    3: "Pausiert",       4: "Fehler",
    5: "Schläft/Lädt",  6: "Gesperrt",
    7: "Randfahrt",      8: "Geplant",
    0: "Unbekannt",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """LawnMower Entities für alle STIGA-Roboter einrichten."""
    coordinator: StigaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        StigaLawnMower(coordinator, device)
        for device in coordinator.data.get("devices", [])
        if _dev_uuid(device)
    ]
    async_add_entities(entities)


class StigaLawnMower(CoordinatorEntity[StigaDataUpdateCoordinator], LawnMowerEntity):
    """Repräsentiert einen STIGA Mäh-Roboter in Home Assistant."""

    _attr_has_entity_name = True
    _attr_name = None  # Name kommt vom Device
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.DOCK
        | LawnMowerEntityFeature.PAUSE
    )

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        self._device   = device
        self._uuid     = _dev_uuid(device)
        attrs          = device.get("attributes") or {}
        self._dev_name = attrs.get("name") or self._uuid
        self._serial   = attrs.get("serial_number", "")
        self._product  = attrs.get("product_code", "")
        self._dev_type = attrs.get("device_type", "")

        self._attr_unique_id = f"stiga_{self._uuid}"

    # ------------------------------------------------------------------ Device

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._dev_name,
            manufacturer="STIGA",
            model=self._product or self._dev_type,
            serial_number=self._serial,
        )

    # ------------------------------------------------------------------ Zustand

    @property
    def _status(self) -> dict:
        return self.coordinator.data.get("statuses", {}).get(self._uuid, {})

    @property
    def activity(self) -> LawnMowerActivity | None:
        mode = self._status.get("mowing_mode")
        if mode is None:
            return None
        activity = MOWING_MODE_TO_ACTIVITY.get(mode)
        if activity is None:
            _LOGGER.debug("Unbekannter mowingMode: %r – als DOCKED behandelt", mode)
            return LawnMowerActivity.DOCKED
        return activity

    @property
    def battery_level(self) -> int | None:
        return self._status.get("battery_level")

    # ------------------------------------------------------------------ Attribute

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._status
        mode = s.get("mowing_mode")
        attrs: dict[str, Any] = {
            ATTR_MOWING_MODE_RAW:   mode,
            "mowing_mode_label":    MOWING_MODE_LABELS.get(mode, str(mode) if mode else "—"),
            ATTR_SERIAL_NUMBER:     self._serial,
            ATTR_PRODUCT_CODE:      self._product,
            ATTR_DEVICE_TYPE:       self._dev_type,
        }

        # Batterie-Details
        for key, attr in (
            ("battery_voltage",    ATTR_BATTERY_VOLTAGE),
            ("battery_capacity",   ATTR_BATTERY_CAPACITY),
            ("battery_remaining",  ATTR_BATTERY_REMAINING),
            ("battery_cycles",     ATTR_BATTERY_CYCLES),
            ("battery_power_w",    ATTR_BATTERY_POWER),
            ("battery_health",     ATTR_BATTERY_HEALTH),
            ("battery_time_left",  ATTR_BATTERY_TIME_LEFT),
            ("battery_current",    ATTR_BATTERY_CURRENT),
            ("error_code",         ATTR_ERROR_CODE),
        ):
            val = s.get(key)
            if val is not None:
                attrs[attr] = val

        # Ladezustand
        if s.get("battery_charging"):
            attrs["battery_charging"] = True

        # Zusätzliche Felder aus der API (z.B. hasData, etc.)
        for k, v in (s.get("extra") or {}).items():
            if v is not None:
                attrs[f"extra_{k}"] = v

        return attrs

    # ------------------------------------------------------------------ Befehle

    async def async_start_mowing(self) -> None:
        """Mähsession starten."""
        try:
            await self.coordinator.api.start_mowing(self._uuid)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Fehler beim Starten: %s", err)

    async def async_dock(self) -> None:
        """Roboter zur Ladestation schicken."""
        try:
            await self.coordinator.api.stop_mowing(self._uuid)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Fehler beim Andocken: %s", err)

    async def async_pause(self) -> None:
        """
        Mähsession pausieren.
        STIGA hat keinen dedizierten Pause-Befehl in der öffentlichen API –
        endsession schickt den Roboter zur Station (nächster sinnvoller Schritt).
        """
        try:
            await self.coordinator.api.stop_mowing(self._uuid)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Fehler beim Pausieren: %s", err)


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
