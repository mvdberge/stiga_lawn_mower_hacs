"""STIGA sensor entities – battery and status as dedicated sensors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTime,
    UnitOfElectricCurrent,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import StigaDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class StigaSensorDescription(SensorEntityDescription):
    """Extended sensor description with API key."""
    status_key: str = ""


SENSOR_DESCRIPTIONS: tuple[StigaSensorDescription, ...] = (
    # Sensors with a standard device_class get their name from HA's built-in translations.
    StigaSensorDescription(
        key="battery_level",
        status_key="battery_level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    StigaSensorDescription(
        key="battery_voltage",
        status_key="battery_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        suggested_display_precision=2,
    ),
    StigaSensorDescription(
        key="battery_power_w",
        status_key="battery_power_w",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
    ),
    StigaSensorDescription(
        key="battery_current",
        status_key="battery_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    StigaSensorDescription(
        key="battery_time_left",
        status_key="battery_time_left",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
    ),
    # Sensors without a device_class use translation_key for localised names.
    StigaSensorDescription(
        key="battery_cycles",
        status_key="battery_cycles",
        translation_key="battery_cycles",
        native_unit_of_measurement="cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=True,
    ),
    StigaSensorDescription(
        key="battery_health",
        status_key="battery_health",
        translation_key="battery_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
    ),
    StigaSensorDescription(
        key="battery_capacity",
        status_key="battery_capacity",
        translation_key="battery_capacity",
        native_unit_of_measurement="mAh",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    StigaSensorDescription(
        key="battery_remaining",
        status_key="battery_remaining",
        translation_key="battery_remaining",
        native_unit_of_measurement="mAh",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        suggested_display_precision=0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities for all STIGA robots."""
    coordinator: StigaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[StigaSensor] = []
    for device in coordinator.data.get("devices", []):
        uuid = _dev_uuid(device)
        if not uuid:
            continue
        for description in SENSOR_DESCRIPTIONS:
            entities.append(StigaSensor(coordinator, device, description))

    async_add_entities(entities)


class StigaSensor(CoordinatorEntity[StigaDataUpdateCoordinator], SensorEntity):
    """A single STIGA sensor (e.g. battery level, voltage, ...)."""

    _attr_has_entity_name = True
    entity_description: StigaSensorDescription

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
        description: StigaSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        attrs = device.get("attributes") or {}
        self._uuid    = _dev_uuid(device)
        self._serial  = attrs.get("serial_number", "")
        self._product = attrs.get("product_code", "")
        self._dev_name = attrs.get("name") or self._uuid

        self._attr_unique_id = f"stiga_{self._uuid}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._dev_name,
            manufacturer="STIGA",
            model=self._product,
            serial_number=self._serial,
        )

    @property
    def available(self) -> bool:
        return super().available and self._uuid in self.coordinator.data.get("statuses", {})

    @property
    def native_value(self) -> Any:
        status = self.coordinator.data.get("statuses", {}).get(self._uuid, {})
        return status.get(self.entity_description.status_key)


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
