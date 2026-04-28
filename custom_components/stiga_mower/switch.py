"""STIGA switch entities — boolean settings sent via MQTT."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import StigaConfigEntry
from .const import DOMAIN
from .coordinator import StigaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class StigaSwitchDescription(SwitchEntityDescription):
    """Extended switch description.

    `settings_key`: key used in encode_settings_update and live_settings dict.
    """

    settings_key: str = ""


SWITCH_DESCRIPTIONS: tuple[StigaSwitchDescription, ...] = (
    StigaSwitchDescription(
        key="rain_sensor_enabled",
        translation_key="rain_sensor_enabled",
        settings_key="rain_sensor_enabled",
        entity_category=EntityCategory.CONFIG,
    ),
    StigaSwitchDescription(
        key="anti_theft",
        translation_key="anti_theft",
        settings_key="anti_theft",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    StigaSwitchDescription(
        key="keyboard_lock",
        translation_key="keyboard_lock",
        settings_key="keyboard_lock",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    StigaSwitchDescription(
        key="push_notifications",
        translation_key="push_notifications",
        settings_key="push_notifications",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    StigaSwitchDescription(
        key="obstacle_notifications",
        translation_key="obstacle_notifications",
        settings_key="obstacle_notifications",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    StigaSwitchDescription(
        key="smart_cutting_height",
        translation_key="smart_cutting_height",
        settings_key="smart_cutting_height",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    StigaSwitchDescription(
        key="long_exit",
        translation_key="long_exit",
        settings_key="long_exit",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities for all STIGA robots."""
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[StigaSwitch] = []
        for device in coordinator.data.get("devices", []):
            uuid = _dev_uuid(device)
            if not uuid:
                continue
            for description in SWITCH_DESCRIPTIONS:
                key = (uuid, description.key)
                if key in known:
                    continue
                known.add(key)
                new_entities.append(StigaSwitch(coordinator, device, description))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))
    _add_new_entities()


class StigaSwitch(CoordinatorEntity[StigaDataUpdateCoordinator], SwitchEntity):
    """A boolean STIGA setting controllable via MQTT."""

    _attr_has_entity_name = True
    entity_description: StigaSwitchDescription

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
        description: StigaSwitchDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        attrs = device.get("attributes") or {}
        self._uuid = attrs.get("uuid", "")
        self._mac = attrs.get("mac_address", "")
        self._attr_unique_id = f"stiga_{self._uuid}_{description.key}"

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
        if fw := a.get("firmware_version"):
            info["sw_version"] = fw
        if mac := a.get("mac_address"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        return info

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._current_value() is not None

    def _current_value(self) -> bool | None:
        key = self.entity_description.settings_key
        live = self.coordinator.data.get("live_settings", {}).get(self._mac)
        if live is not None and key in live:
            return bool(live[key])
        return None

    @property
    def is_on(self) -> bool | None:
        return self._current_value()

    async def _send(self, value: bool) -> None:
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.connected or not self._mac:
            raise HomeAssistantError(
                f"Cannot set {self.entity_description.key}: MQTT not connected"
            )
        settings = {self.entity_description.settings_key: value}
        try:
            await mqtt.cmd_settings_update(self._mac, settings)
        except Exception as err:
            raise HomeAssistantError(f"Could not set {self.entity_description.key}: {err}") from err

    async def async_turn_on(self, **kwargs) -> None:
        await self._send(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._send(False)


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
