"""STIGA LawnMower entity for Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import StigaConfigEntry
from .const import (
    DOMAIN,
    ATTR_SERIAL_NUMBER,
    ATTR_PRODUCT_CODE,
    ATTR_DEVICE_TYPE,
    ATTR_MOWING_MODE_RAW,
    ATTR_ERROR_CODE,
    ATTR_ERROR_DESCRIPTION,
    ATTR_LAST_USED,
    ATTR_LTE_VERSION,
    ATTR_TOTAL_WORK_TIME,
    ATTR_RAIN_SENSOR,
    ATTR_BASE_UUID,
    ATTR_WORKING_DAYTIMES_ON,
    ERROR_INFO_CODES,
)
from .coordinator import StigaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

# currentAction describes what the robot is doing RIGHT NOW and takes priority.
# Maps each value to (LawnMowerActivity, human-readable label).
#
# Reference for the state semantics (names/codes reverse engineered from the
# protobuf MQTT payload): https://github.com/matthewgream/stiga-api
_CURRENT_ACTION: dict[str, tuple[LawnMowerActivity, str]] = {
    # Mowing
    "MOWING":                (LawnMowerActivity.MOWING,  "Mowing"),
    "WORKING":               (LawnMowerActivity.MOWING,  "Mowing"),
    "BORDER":                (LawnMowerActivity.MOWING,  "Border mowing"),
    "BORDER_CUTTING":        (LawnMowerActivity.MOWING,  "Border mowing"),
    "CUTTING_BORDER":        (LawnMowerActivity.MOWING,  "Border mowing"),
    "PLANNING_ONGOING":      (LawnMowerActivity.MOWING,  "Planning"),
    "REACHING_FIRST_POINT":  (LawnMowerActivity.MOWING,  "Heading to start point"),
    "NAVIGATING_TO_AREA":    (LawnMowerActivity.MOWING,  "Navigating to zone"),
    # Returning to dock
    "GOING_HOME":            (LawnMowerActivity.PAUSED,  "Returning to dock"),
    "BACK_HOME":             (LawnMowerActivity.PAUSED,  "Returning to dock"),
    "BACK_HOME_MANUAL":      (LawnMowerActivity.PAUSED,  "Returning to dock"),
    # Docked
    "AT_HOME":               (LawnMowerActivity.DOCKED,  "At home"),
    "CHARGING":              (LawnMowerActivity.DOCKED,  "Charging"),
    "UPDATING":              (LawnMowerActivity.DOCKED,  "Updating firmware"),
    "STORING_DATA":          (LawnMowerActivity.DOCKED,  "Storing data"),
    # Paused / idle outside the dock
    "PAUSE":                 (LawnMowerActivity.PAUSED,  "Paused"),
    "WAITING":               (LawnMowerActivity.PAUSED,  "Waiting for command"),
    "WAITING_FOR_COMMAND":   (LawnMowerActivity.PAUSED,  "Waiting for command"),
    "STOPPED":               (LawnMowerActivity.PAUSED,  "Stopped"),
    "NONE":                  (LawnMowerActivity.PAUSED,  "Idle"),
    "CALIBRATION":           (LawnMowerActivity.PAUSED,  "Calibrating"),
    "BLADES_CALIBRATING":    (LawnMowerActivity.PAUSED,  "Calibrating blades"),
    # Error
    "ERROR":                 (LawnMowerActivity.ERROR,   "Error"),
    "ROBOT_ERROR":           (LawnMowerActivity.ERROR,   "Error"),
    "BLOCKED":               (LawnMowerActivity.ERROR,   "Blocked"),
    "LID_OPEN":              (LawnMowerActivity.ERROR,   "Lid open"),
    "STARTUP_REQUIRED":      (LawnMowerActivity.ERROR,   "Startup required"),
}

# mowingMode describes HOW the session was started (fallback when currentAction is absent).
# Actual API values: strings for vista_robot, integers for older models.
#
# Note: SCHEDULED and IDLE are intentionally missing – they only say "a schedule
# is configured" / "no active session" and can mean either "waiting in dock" or
# "stopped outside". Without a stronger signal (isDocked or currentAction) we
# can't tell, so the activity falls through to None (unknown).
MOWING_MODE_TO_ACTIVITY: dict[Any, LawnMowerActivity] = {
    "WORKING":    LawnMowerActivity.MOWING,
    "BORDER":     LawnMowerActivity.MOWING,
    "MANUAL":     LawnMowerActivity.MOWING,
    "GOING_HOME": LawnMowerActivity.PAUSED,  # no RETURNING in HA, closest matching state
    "PAUSE":      LawnMowerActivity.PAUSED,
    "CHARGING":   LawnMowerActivity.DOCKED,
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

# Modes that are known but ambiguous without a stronger signal – suppress the
# "unknown mode" warning for these.
_AMBIGUOUS_MODES: frozenset = frozenset({"SCHEDULED", "IDLE"})

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
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LawnMower entities for all STIGA robots."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[StigaLawnMower] = []
        for device in coordinator.data.get("devices", []):
            uuid = _dev_uuid(device)
            if not uuid or uuid in known:
                continue
            known.add(uuid)
            new_entities.append(StigaLawnMower(coordinator, device))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))
    _add_new_entities()


class StigaLawnMower(CoordinatorEntity[StigaDataUpdateCoordinator], LawnMowerEntity):
    """Represents a STIGA robotic lawn mower in Home Assistant."""

    _attr_has_entity_name = True
    _attr_name = None  # Name comes from the device
    # No PAUSE: the public STIGA API has no pause command – endsession sends
    # the robot back to the dock, which is the same as DOCK.
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.DOCK
    )

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        self._uuid     = _dev_uuid(device)
        self._attr_unique_id = f"stiga_{self._uuid}"

    # ------------------------------------------------------------------ Device

    def _device_attrs(self) -> dict:
        """Latest device attributes from the coordinator (refreshed each cycle)."""
        for d in self.coordinator.data.get("devices", []):
            if _dev_uuid(d) == self._uuid:
                return d.get("attributes") or {}
        return {}

    @property
    def device_info(self) -> DeviceInfo:
        a = self._device_attrs()
        info = DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=a.get("name") or self._uuid,
            manufacturer="STIGA",
            model=a.get("product_code") or a.get("device_type") or "",
            serial_number=a.get("serial_number") or "",
        )
        if fw := a.get("firmware_version"):
            info["sw_version"] = fw
        if mac := a.get("mac_address"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        return info

    # ------------------------------------------------------------------ State

    @property
    def _status(self) -> dict:
        return self.coordinator.data.get("statuses", {}).get(self._uuid, {})

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        status = self.coordinator.data.get("statuses", {}).get(self._uuid)
        if not status:
            return False
        # STIGA signals "no fresh telemetry" via hasData == false. Treat
        # missing/None as available for backwards compatibility with older
        # device types that don't expose this flag.
        return status.get("has_data") is not False

    @property
    def activity(self) -> LawnMowerActivity | None:
        s = self._status
        if not s:
            return None

        # Strongest signal: STIGA sends isDocked: true when the robot is parked
        # in its charging station. Overrides all other state fields.
        if s.get("is_docked") is True:
            return LawnMowerActivity.DOCKED

        # currentAction reflects what the robot is doing right now.
        action = s.get("current_action")
        if isinstance(action, str):
            entry = _CURRENT_ACTION.get(action.upper())
            if entry is not None:
                return entry[0]

        # Fall back to mowingMode (describes how the session was started).
        mode = s.get("mowing_mode")
        if mode is not None:
            activity = MOWING_MODE_TO_ACTIVITY.get(mode)
            if activity is None and isinstance(mode, str):
                activity = MOWING_MODE_TO_ACTIVITY.get(mode.upper())
            if activity is not None:
                return activity
            mode_key = mode.upper() if isinstance(mode, str) else mode
            if mode_key not in _AMBIGUOUS_MODES:
                _LOGGER.warning(
                    "Unknown mowingMode %r / currentAction %r – please report as a GitHub issue",
                    mode, action,
                )

        # No currentAction, no confirming isDocked, and mowingMode is either
        # missing or ambiguous (e.g. SCHEDULED while stopped outside). Report
        # the mower as paused rather than leaving the state unknown.
        return LawnMowerActivity.PAUSED

    @property
    def battery_level(self) -> int | None:
        return self._status.get("battery_level")

    # ------------------------------------------------------------------ Attributes

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._status
        a = self._device_attrs()
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
            ATTR_SERIAL_NUMBER:      a.get("serial_number", ""),
            ATTR_PRODUCT_CODE:       a.get("product_code", ""),
            ATTR_DEVICE_TYPE:        a.get("device_type", ""),
        }

        if (ec := s.get("error_code")) is not None:
            attrs[ATTR_ERROR_CODE] = ec
            if (desc := _lookup_error_description(ec)) is not None:
                attrs[ATTR_ERROR_DESCRIPTION] = desc

        if s.get("battery_charging"):
            attrs["battery_charging"] = True

        # Extra fields exposed by the (undocumented) /api/garage endpoint —
        # only present when that endpoint is reachable, otherwise omitted.
        if (last_used := a.get("last_used")) is not None:
            attrs[ATTR_LAST_USED] = last_used
        if (twt := a.get("total_work_time")) is not None:
            attrs[ATTR_TOTAL_WORK_TIME] = twt
        if (base := a.get("base_uuid")) is not None:
            attrs[ATTR_BASE_UUID] = base
        if isinstance(state := a.get("state"), dict) and (lte := state.get("lteVersion")):
            attrs[ATTR_LTE_VERSION] = lte
        if isinstance(daytimes := a.get("working_daytimes"), dict):
            attrs[ATTR_WORKING_DAYTIMES_ON] = bool(daytimes.get("enabled"))
        parsed = _parsed_settings(a)
        if (rs := parsed.get("rain_sensor")) is not None:
            attrs[ATTR_RAIN_SENSOR] = rs

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
        """Send the robot back to the charging dock."""
        try:
            await self.coordinator.api.stop_mowing(self._uuid)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(f"Could not send dock command: {err}") from err


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")


def _parsed_settings(device_attrs: dict) -> dict:
    """Extract attributes.settings[0].parsedSettings safely from /api/garage."""
    settings = device_attrs.get("settings")
    if isinstance(settings, list) and settings:
        first = settings[0] or {}
        ps = first.get("parsedSettings")
        if isinstance(ps, dict):
            return ps
    return {}


def _lookup_error_description(code: Any) -> str | None:
    """Translate a numeric error/info code into a human-readable key."""
    if isinstance(code, int):
        return ERROR_INFO_CODES.get(code)
    if isinstance(code, str):
        try:
            return ERROR_INFO_CODES.get(int(code, 0))
        except ValueError:
            return None
    return None
