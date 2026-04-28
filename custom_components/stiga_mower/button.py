"""STIGA button entities — one-shot MQTT commands."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
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


class _ButtonAction(StrEnum):
    CALIBRATE_BLADES = "calibrate_blades"
    REFRESH_STATUS = "refresh_status"


@dataclass(frozen=True, kw_only=True)
class StigaButtonDescription(ButtonEntityDescription):
    """Extended button description.

    `action`: which command to publish when pressed.
    """

    action: _ButtonAction = _ButtonAction.REFRESH_STATUS


BUTTON_DESCRIPTIONS: tuple[StigaButtonDescription, ...] = (
    StigaButtonDescription(
        key="calibrate_blades",
        translation_key="calibrate_blades",
        action=_ButtonAction.CALIBRATE_BLADES,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    StigaButtonDescription(
        key="refresh_status",
        translation_key="refresh_status",
        action=_ButtonAction.REFRESH_STATUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities for all STIGA robots."""
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[StigaButton] = []
        for device in coordinator.data.get("devices", []):
            uuid = _dev_uuid(device)
            if not uuid:
                continue
            for description in BUTTON_DESCRIPTIONS:
                key = (uuid, description.key)
                if key in known:
                    continue
                known.add(key)
                new_entities.append(StigaButton(coordinator, device, description))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))
    _add_new_entities()


class StigaButton(CoordinatorEntity[StigaDataUpdateCoordinator], ButtonEntity):
    """A one-shot STIGA command button."""

    _attr_has_entity_name = True
    entity_description: StigaButtonDescription

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
        description: StigaButtonDescription,
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

    async def async_press(self) -> None:
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.connected or not self._mac:
            raise HomeAssistantError(
                f"Cannot press {self.entity_description.key}: MQTT not connected"
            )
        action = self.entity_description.action
        try:
            if action == _ButtonAction.CALIBRATE_BLADES:
                await mqtt.cmd_calibrate_blades(self._mac)
            elif action == _ButtonAction.REFRESH_STATUS:
                await mqtt.request_status(self._mac)
        except Exception as err:
            raise HomeAssistantError(
                f"Could not execute {self.entity_description.key}: {err}"
            ) from err


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
