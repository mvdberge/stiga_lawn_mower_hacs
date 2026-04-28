"""STIGA number entities — writable numeric settings sent via MQTT."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import StigaConfigEntry
from .const import DOMAIN
from .coordinator import StigaDataUpdateCoordinator
from .mqtt_constants import CUTTING_HEIGHTS_MM

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class StigaNumberDescription(NumberEntityDescription):
    """Extended number description.

    `settings_key`: key used in encode_settings_update and live_settings dict.
    """

    settings_key: str = ""


NUMBER_DESCRIPTIONS: tuple[StigaNumberDescription, ...] = (
    StigaNumberDescription(
        key="cutting_height",
        translation_key="cutting_height",
        settings_key="cutting_height_mm",
        device_class=NumberDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        native_min_value=min(CUTTING_HEIGHTS_MM),
        native_max_value=max(CUTTING_HEIGHTS_MM),
        native_step=5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities for all STIGA robots."""
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[StigaNumber] = []
        for device in coordinator.data.get("devices", []):
            uuid = _dev_uuid(device)
            if not uuid:
                continue
            for description in NUMBER_DESCRIPTIONS:
                key = (uuid, description.key)
                if key in known:
                    continue
                known.add(key)
                new_entities.append(StigaNumber(coordinator, device, description))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))
    _add_new_entities()


class StigaNumber(CoordinatorEntity[StigaDataUpdateCoordinator], NumberEntity):
    """A writable numeric setting sent to the mower via MQTT."""

    _attr_has_entity_name = True
    entity_description: StigaNumberDescription

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
        description: StigaNumberDescription,
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
        # Settable only when MQTT is live; otherwise read-only from REST status.
        return self._native_value() is not None

    def _native_value(self) -> float | None:
        key = self.entity_description.settings_key
        # Prefer MQTT live_settings (most recent mower-reported value).
        live = self.coordinator.data.get("live_settings", {}).get(self._mac)
        if live and (v := live.get(key)) is not None:
            return float(v)
        # Fall back to REST-enriched status (cutting_height_mm from parsedSettings).
        status = self.coordinator.data.get("statuses", {}).get(self._uuid, {})
        if (v := status.get(key)) is not None:
            return float(v)
        return None

    @property
    def native_value(self) -> float | None:
        return self._native_value()

    async def async_set_native_value(self, value: float) -> None:
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.connected or not self._mac:
            raise HomeAssistantError(
                f"Cannot set {self.entity_description.key}: MQTT not connected"
            )
        settings = {self.entity_description.settings_key: int(value)}
        try:
            await mqtt.cmd_settings_update(self._mac, settings)
        except Exception as err:
            raise HomeAssistantError(f"Could not set {self.entity_description.key}: {err}") from err


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
