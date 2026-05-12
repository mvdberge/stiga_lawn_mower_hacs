"""STIGA select entities — enum settings sent via MQTT."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import StigaConfigEntry
from .const import DOMAIN, split_firmware_version
from .coordinator import StigaDataUpdateCoordinator
from .mqtt_constants import CUTTING_MODES, RAIN_DELAYS_HOURS

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class StigaSelectDescription(SelectEntityDescription):
    """Extended select description.

    `settings_key`: key used in encode_settings_update / live_settings.
    `option_to_wire`: maps option string → value for encode_settings_update.
    `wire_to_option`: reverse mapping for reading current value.
    """

    settings_key: str = ""
    option_to_wire: dict = field(default_factory=dict)
    wire_to_option: dict = field(default_factory=dict)
    # Wire value to assume when the key is missing from a populated
    # live_settings entry. STIGA's firmware uses proto3 encoding, which omits
    # scalar fields whose value equals the wire default (varint 0). Without
    # this fallback the dependent select stays permanently unavailable as
    # long as the setting sits at its default value. Leave ``None`` for
    # selects whose wire index 0 is not a valid option (e.g. cutting_mode,
    # which is not decoded from SETTINGS today).
    wire_default: Any = None


def _reverse(d: dict) -> dict:
    return {v: k for k, v in d.items()}


SELECT_DESCRIPTIONS: tuple[StigaSelectDescription, ...] = (
    StigaSelectDescription(
        key="cutting_mode",
        translation_key="cutting_mode",
        settings_key="cutting_mode",
        options=list(CUTTING_MODES),
        option_to_wire=dict(CUTTING_MODES),
        wire_to_option=_reverse(CUTTING_MODES),
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    StigaSelectDescription(
        key="rain_sensor_delay",
        translation_key="rain_sensor_delay",
        settings_key="rain_sensor_delay_h",
        # Options are displayed as strings; the value is hours.
        options=[str(h) for h in sorted(RAIN_DELAYS_HOURS)],
        # option_to_wire: "4" -> 4 (hours int; encode_settings_update maps h -> wire idx)
        option_to_wire={str(h): h for h in RAIN_DELAYS_HOURS},
        # wire_to_option: hours int -> str (live_settings stores hours directly)
        wire_to_option={h: str(h) for h in RAIN_DELAYS_HOURS},
        # Wire index 0 (= 4 hours) is the proto3 default and gets omitted on
        # the wire. Without this fallback the select stays unavailable while
        # the robot sits at the factory default.
        wire_default=4,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities for all STIGA robots."""
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[StigaSelect | StigaScheduleModeSelect] = []
        for device in coordinator.data.get("devices", []):
            uuid = _dev_uuid(device)
            if not uuid:
                continue
            for description in SELECT_DESCRIPTIONS:
                key = (uuid, description.key)
                if key in known:
                    continue
                known.add(key)
                new_entities.append(StigaSelect(coordinator, device, description))
            sched_key = (uuid, "schedule_mode")
            if sched_key not in known:
                known.add(sched_key)
                new_entities.append(StigaScheduleModeSelect(coordinator, device))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))
    _add_new_entities()


class StigaSelect(CoordinatorEntity[StigaDataUpdateCoordinator], SelectEntity):
    """An enum STIGA setting controllable via MQTT."""

    _attr_has_entity_name = True
    entity_description: StigaSelectDescription

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
        description: StigaSelectDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        attrs = device.get("attributes") or {}
        self._uuid = attrs.get("uuid", "")
        self._mac = attrs.get("mac_address", "")
        self._attr_unique_id = f"stiga_{self._uuid}_{description.key}"
        self._attr_options = list(description.options)

    def _device_attrs(self) -> dict:
        for d in self.coordinator.data.get("devices", []):
            if _dev_uuid(d) == self._uuid:
                return d.get("attributes") or {}
        return {}

    @property
    def device_info(self) -> DeviceInfo:
        a = self._device_attrs()
        meta = self.coordinator.data.get("meta", {}).get(self._uuid, {})
        info = DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=a.get("name") or self._uuid,
            manufacturer="STIGA",
            model=meta.get("model_name") or a.get("product_code") or a.get("device_type") or "",
            serial_number=a.get("serial_number") or "",
        )
        hw, fw, _build = split_firmware_version(a.get("firmware_version"))
        if fw:
            info["sw_version"] = fw
        if hw and hw != fw:
            info["hw_version"] = hw
        if mac := a.get("mac_address"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        return info

    @property
    def available(self) -> bool:
        if not self.coordinator.data:
            return False
        # Both value source (live_settings) and write path go via MQTT — REST
        # freshness is irrelevant here. Mirrors StigaSwitch.available.
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.connected:
            return False
        return self.current_option is not None

    @property
    def current_option(self) -> str | None:
        key = self.entity_description.settings_key
        live = self.coordinator.data.get("live_settings", {}).get(self._mac)
        if live is None:
            return None
        # A populated entry means a SETTINGS frame has arrived. Proto3 omits
        # scalar fields at their wire default, so a missing key for a
        # description with a configured ``wire_default`` resolves to that
        # default rather than to "unknown" — otherwise the select stays
        # permanently unavailable whenever the robot sits at index 0.
        raw = live.get(key, self.entity_description.wire_default)
        if raw is None:
            return None
        return self.entity_description.wire_to_option.get(raw)

    async def async_select_option(self, option: str) -> None:
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.connected or not self._mac:
            raise HomeAssistantError(
                f"Cannot set {self.entity_description.key}: MQTT not connected"
            )
        wire_value = self.entity_description.option_to_wire.get(option)
        if wire_value is None:
            raise HomeAssistantError(f"Unknown option {option!r}")
        settings: dict = {self.entity_description.settings_key: wire_value}
        if self.entity_description.settings_key == "rain_sensor_delay_h":
            live = self.coordinator.data.get("live_settings", {}).get(self._mac) or {}
            # The rain submsg is atomic: include the current enabled flag so the
            # firmware doesn't reset it when only the delay index changes.
            if (enabled := live.get("rain_sensor_enabled")) is not None:
                settings["rain_sensor_enabled"] = enabled
            # The app always sends zone_cutting_height_enabled alongside any
            # rain-sensor write (cutting submsg is also atomic on this firmware).
            if (zch := live.get("zone_cutting_height_enabled")) is not None:
                settings["zone_cutting_height_enabled"] = zch
        try:
            await mqtt.cmd_settings_update(self._mac, settings)
        except Exception as err:
            raise HomeAssistantError(f"Could not set {self.entity_description.key}: {err}") from err
        # Optimistic update: the firmware omits enum fields at their proto3 wire
        # default (e.g. rain delay index 0 = 4 h) from SETTINGS responses. Apply
        # the selected value immediately so the entity reflects the command sent.
        self.coordinator.apply_live_settings(
            self._mac, {self.entity_description.settings_key: wire_value}
        )


SCHEDULE_MODE_AUTO = "auto"
SCHEDULE_MODE_MANUAL = "manual"


class StigaScheduleModeSelect(CoordinatorEntity[StigaDataUpdateCoordinator], SelectEntity):
    """Select that switches between manual and scheduled (auto) mowing mode.

    Reads the ``enabled`` flag from ``live_schedule[mac]`` and sends only
    field 1 of SCHEDULING_SETTINGS_UPDATE so the stored time-window blob is
    left intact.  Lives in the default "Controls" category (no
    ``entity_category``) — this is a user-facing operating mode, not a
    configuration tweak.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "schedule_mode"

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        attrs = device.get("attributes") or {}
        self._uuid = attrs.get("uuid", "")
        self._mac = attrs.get("mac_address", "")
        self._attr_unique_id = f"stiga_{self._uuid}_schedule_mode"
        self._attr_options = [SCHEDULE_MODE_MANUAL, SCHEDULE_MODE_AUTO]

    def _device_attrs(self) -> dict:
        for d in self.coordinator.data.get("devices", []):
            if _dev_uuid(d) == self._uuid:
                return d.get("attributes") or {}
        return {}

    @property
    def device_info(self) -> DeviceInfo:
        a = self._device_attrs()
        meta = self.coordinator.data.get("meta", {}).get(self._uuid, {})
        info = DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=a.get("name") or self._uuid,
            manufacturer="STIGA",
            model=meta.get("model_name") or a.get("product_code") or a.get("device_type") or "",
            serial_number=a.get("serial_number") or "",
        )
        hw, fw, _build = split_firmware_version(a.get("firmware_version"))
        if fw:
            info["sw_version"] = fw
        if hw and hw != fw:
            info["hw_version"] = hw
        if mac := a.get("mac_address"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        return info

    def _schedule_enabled(self) -> bool | None:
        sched = self.coordinator.data.get("live_schedule", {}).get(self._mac)
        if sched is None:
            return None
        # Proto3 default-omission: a disabled schedule (field 1 = False) is
        # omitted from the wire, so a missing ``enabled`` key in a populated
        # entry means False rather than "unknown".
        return bool(sched.get("enabled"))

    @property
    def available(self) -> bool:
        if not self.coordinator.data:
            return False
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.connected:
            return False
        return self._schedule_enabled() is not None

    @property
    def current_option(self) -> str | None:
        enabled = self._schedule_enabled()
        if enabled is None:
            return None
        return SCHEDULE_MODE_AUTO if enabled else SCHEDULE_MODE_MANUAL

    async def async_select_option(self, option: str) -> None:
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.connected or not self._mac:
            raise HomeAssistantError("Cannot set schedule mode: MQTT not connected")
        if option not in (SCHEDULE_MODE_MANUAL, SCHEDULE_MODE_AUTO):
            raise HomeAssistantError(f"Unknown option {option!r}")
        try:
            await mqtt.cmd_schedule_set_enabled(self._mac, option == SCHEDULE_MODE_AUTO)
        except Exception as err:
            raise HomeAssistantError(f"Could not set schedule mode: {err}") from err


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
