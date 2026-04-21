"""STIGA LawnMower entity for Home Assistant."""

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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    ATTR_SERIAL_NUMBER,
    ATTR_PRODUCT_CODE,
    ATTR_DEVICE_TYPE,
    ATTR_MOWING_MODE_RAW,
    ATTR_ERROR_CODE,
)
from .coordinator import StigaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# currentAction describes what the robot is doing RIGHT NOW and takes priority.
# Maps each value to (LawnMowerActivity, human-readable label).
_CURRENT_ACTION: dict[str, tuple[LawnMowerActivity, str]] = {
    "MOWING":          (LawnMowerActivity.MOWING,  "Mowing"),
    "BORDER_CUTTING":  (LawnMowerActivity.MOWING,  "Border mowing"),
    "BORDER":          (LawnMowerActivity.MOWING,  "Border mowing"),
    "WORKING":         (LawnMowerActivity.MOWING,  "Mowing"),
    "GOING_HOME":      (LawnMowerActivity.PAUSED,  "Returning to dock"),
    "PAUSE":           (LawnMowerActivity.PAUSED,  "Paused"),
    "CHARGING":        (LawnMowerActivity.DOCKED,  "Charging"),
    "WAITING":         (LawnMowerActivity.DOCKED,  "Waiting"),
    "STOPPED":         (LawnMowerActivity.DOCKED,  "Stopped"),
    "NONE":            (LawnMowerActivity.DOCKED,  "Idle"),
    "ERROR":           (LawnMowerActivity.ERROR,   "Error"),
}

# mowingMode describes HOW the session was started (fallback when currentAction is absent).
# Actual API values: strings for vista_robot, integers for older models.
MOWING_MODE_TO_ACTIVITY: dict[Any, LawnMowerActivity] = {
    "WORKING":    LawnMowerActivity.MOWING,
    "BORDER":     LawnMowerActivity.MOWING,
    "MANUAL":     LawnMowerActivity.MOWING,
    "GOING_HOME": LawnMowerActivity.PAUSED,  # no RETURNING in HA, closest matching state
    "PAUSE":      LawnMowerActivity.PAUSED,
    "IDLE":       LawnMowerActivity.DOCKED,
    "CHARGING":   LawnMowerActivity.DOCKED,
    "SCHEDULED":  LawnMowerActivity.DOCKED,
    "SLEEPING":   LawnMowerActivity.DOCKED,
    "UPDATING":   LawnMowerActivity.DOCKED,
    "ERROR":      LawnMowerActivity.ERROR,
    "LOCKED":     LawnMowerActivity.ERROR,
    # Integer codes (older autonomous_robot models)
    1: LawnMowerActivity.MOWING,   7: LawnMowerActivity.MOWING,
    2: LawnMowerActivity.PAUSED,   3: LawnMowerActivity.PAUSED,
    4: LawnMowerActivity.ERROR,    6: LawnMowerActivity.ERROR,
    5: LawnMowerActivity.DOCKED,   8: LawnMowerActivity.DOCKED,
    0: LawnMowerActivity.DOCKED,
}

MOWING_MODE_LABELS: dict[Any, str] = {
    "WORKING":    "Mowing",
    "BORDER":     "Border mowing",
    "MANUAL":     "Manual",
    "GOING_HOME": "Returning to dock",
    "PAUSE":      "Paused",
    "IDLE":       "Ready",
    "CHARGING":   "Charging",
    "SCHEDULED":  "Scheduled",
    "SLEEPING":   "Sleeping",
    "UPDATING":   "Updating",
    "ERROR":      "Error",
    "LOCKED":     "Locked",
    1: "Mowing",             2: "Returning",
    3: "Paused",             4: "Error",
    5: "Sleeping/Charging",  6: "Locked",
    7: "Border mowing",      8: "Scheduled",
    0: "Unknown",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LawnMower entities for all STIGA robots."""
    coordinator: StigaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        StigaLawnMower(coordinator, device)
        for device in coordinator.data.get("devices", [])
        if _dev_uuid(device)
    ]
    async_add_entities(entities)


class StigaLawnMower(CoordinatorEntity[StigaDataUpdateCoordinator], LawnMowerEntity):
    """Represents a STIGA robotic lawn mower in Home Assistant."""

    _attr_has_entity_name = True
    _attr_name = None  # Name comes from the device
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

    # ------------------------------------------------------------------ State

    @property
    def _status(self) -> dict:
        return self.coordinator.data.get("statuses", {}).get(self._uuid, {})

    @property
    def activity(self) -> LawnMowerActivity | None:
        s = self._status

        # currentAction reflects what the robot is doing right now – use it first.
        action = s.get("current_action")
        if isinstance(action, str):
            entry = _CURRENT_ACTION.get(action.upper())
            if entry is not None:
                return entry[0]

        # Fall back to mowingMode (describes how the session was started).
        mode = s.get("mowing_mode")
        if mode is None:
            return None
        activity = MOWING_MODE_TO_ACTIVITY.get(mode)
        if activity is None and isinstance(mode, str):
            activity = MOWING_MODE_TO_ACTIVITY.get(mode.upper())
        if activity is None:
            _LOGGER.warning(
                "Unknown mowingMode %r / currentAction %r – please report as a GitHub issue",
                mode, action,
            )
            return None
        return activity

    @property
    def battery_level(self) -> int | None:
        return self._status.get("battery_level")

    # ------------------------------------------------------------------ Attributes

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._status
        mode = s.get("mowing_mode")
        action = s.get("current_action")
        entry = _CURRENT_ACTION.get(action.upper()) if isinstance(action, str) else None
        label = (
            entry[1] if entry is not None
            else MOWING_MODE_LABELS.get(mode, str(mode) if mode else "—")
        )
        attrs: dict[str, Any] = {
            ATTR_MOWING_MODE_RAW:    mode,
            "current_action_raw":    action,
            "mowing_mode_label":     label,
            ATTR_SERIAL_NUMBER:      self._serial,
            ATTR_PRODUCT_CODE:       self._product,
            ATTR_DEVICE_TYPE:        self._dev_type,
        }

        if (ec := s.get("error_code")) is not None:
            attrs[ATTR_ERROR_CODE] = ec

        if s.get("battery_charging"):
            attrs["battery_charging"] = True

        for k, v in (s.get("extra") or {}).items():
            if v is not None:
                attrs[f"extra_{k}"] = v

        return attrs

    # ------------------------------------------------------------------ Commands

    async def async_start_mowing(self) -> None:
        """Start a mowing session."""
        try:
            await self.coordinator.api.start_mowing(self._uuid)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(f"Could not start mowing: {err}") from err

    async def async_dock(self) -> None:
        """Send the robot to the charging dock."""
        await self._stop_session("dock")

    async def async_pause(self) -> None:
        """
        Pause the mowing session.
        STIGA has no dedicated pause command in the public API –
        endsession sends the robot to the dock (closest sensible action).
        """
        await self._stop_session("pause")

    async def _stop_session(self, context: str) -> None:
        try:
            await self.coordinator.api.stop_mowing(self._uuid)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(f"Could not send {context} command: {err}") from err


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
