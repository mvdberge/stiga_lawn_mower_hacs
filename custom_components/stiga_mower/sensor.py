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
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfArea,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import StigaConfigEntry
from .const import DOMAIN, split_firmware_version
from .coordinator import StigaDataUpdateCoordinator

PARALLEL_UPDATES = 1

# MQTT-only sensors that require a live connection to be available
_MQTT_ONLY_SENSOR_KEYS = frozenset(
    (
        "current_zone",
        "zone_completed_pct",
        "garden_completed_pct",
        "satellites",
        "rtk_quality_pct",
        "gps_quality",
        "rsrp",
        "rsrq",
        "signal_quality_pct",
    )
)


@dataclass(frozen=True, kw_only=True)
class StigaSensorDescription(SensorEntityDescription):
    """Extended sensor description with API key.

    `source` selects where the value lives in `coordinator.data`:
      - "status": per-cycle live data from `/mqttstatus` (battery, etc.)
      - "meta":   one-shot static data from setup (perimeter, model name)
    """

    status_key: str = ""
    source: str = "status"


SENSOR_DESCRIPTIONS: tuple[StigaSensorDescription, ...] = (
    # Primary user-facing sensor – no category, enabled by default.
    StigaSensorDescription(
        key="battery_level",
        status_key="battery_level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # Diagnostic sensors useful enough to stay enabled by default.
    StigaSensorDescription(
        key="battery_time_left",
        status_key="battery_time_left",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    StigaSensorDescription(
        key="battery_power_w",
        status_key="battery_power_w",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    StigaSensorDescription(
        key="battery_health",
        status_key="battery_health",
        translation_key="battery_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Low-level diagnostics – disabled by default to reduce entity noise.
    StigaSensorDescription(
        key="battery_voltage",
        status_key="battery_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
    ),
    StigaSensorDescription(
        key="battery_current",
        status_key="battery_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    StigaSensorDescription(
        key="battery_cycles",
        status_key="battery_cycles",
        translation_key="battery_cycles",
        native_unit_of_measurement="cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    StigaSensorDescription(
        key="battery_capacity",
        status_key="battery_capacity",
        translation_key="battery_capacity",
        native_unit_of_measurement="mAh",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    StigaSensorDescription(
        key="battery_remaining",
        status_key="battery_remaining",
        translation_key="battery_remaining",
        native_unit_of_measurement="mAh",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
    ),
    # Cutting height as configured in the STIGA.GO app. Read-only — writing
    # would require MQTT, which this integration does not implement.
    StigaSensorDescription(
        key="cutting_height",
        status_key="cutting_height_mm",
        translation_key="cutting_height",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    StigaSensorDescription(
        key="total_work_time",
        status_key="total_work_time",
        translation_key="total_work_time",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Docking-station firmware (`attributes.settings[0].docking_version`).
    # Tracked separately from the robot's own `firmware_version` because the
    # cloud reports them in unrelated fields.
    StigaSensorDescription(
        key="dock_firmware",
        status_key="dock_firmware",
        translation_key="dock_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # Static perimeter sensors – one-shot values fetched at setup from the
    # undocumented /api/perimeters endpoint. Unavailable when /perimeters
    # cannot be reached or when the user hasn't drawn a perimeter yet.
    StigaSensorDescription(
        key="garden_area",
        status_key="garden_area_m2",
        source="meta",
        translation_key="garden_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        device_class=SensorDeviceClass.AREA,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
    ),
    StigaSensorDescription(
        key="zone_count",
        status_key="zone_count",
        source="meta",
        translation_key="zone_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    StigaSensorDescription(
        key="obstacle_count",
        status_key="obstacle_count",
        source="meta",
        translation_key="obstacle_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    StigaSensorDescription(
        key="obstacle_area",
        status_key="obstacle_area_m2",
        source="meta",
        translation_key="obstacle_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        device_class=SensorDeviceClass.AREA,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
    ),
    # ---- MQTT live sensors — only populated when MQTT is connected ----
    StigaSensorDescription(
        key="current_zone",
        status_key="current_zone",
        translation_key="current_zone",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    StigaSensorDescription(
        key="zone_completed_pct",
        status_key="zone_completed_pct",
        translation_key="zone_completed_pct",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    StigaSensorDescription(
        key="garden_completed_pct",
        status_key="garden_completed_pct",
        translation_key="garden_completed_pct",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # GPS / RTK diagnostics
    StigaSensorDescription(
        key="satellites",
        status_key="satellites",
        translation_key="satellites",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    StigaSensorDescription(
        key="rtk_quality_pct",
        status_key="rtk_quality_pct",
        translation_key="rtk_quality_pct",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    StigaSensorDescription(
        key="gps_quality",
        status_key="gps_quality",
        translation_key="gps_quality",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # Network / cellular signal diagnostics
    StigaSensorDescription(
        key="rsrp",
        status_key="rsrp",
        translation_key="rsrp",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    StigaSensorDescription(
        key="rsrq",
        status_key="rsrq",
        translation_key="rsrq",
        native_unit_of_measurement="dB",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    StigaSensorDescription(
        key="signal_quality_pct",
        status_key="signal_quality_pct",
        translation_key="signal_quality_pct",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities for all STIGA robots.

    Track per `(uuid, description.key)` so that integration upgrades that add
    new sensor descriptions automatically create the corresponding entities
    on the next coordinator update — no need to remove and re-add the
    integration.
    """
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[StigaSensor] = []
        for device in coordinator.data.get("devices", []):
            uuid = _dev_uuid(device)
            if not uuid:
                continue
            for description in SENSOR_DESCRIPTIONS:
                key = (uuid, description.key)
                if key in known:
                    continue
                known.add(key)
                new_entities.append(StigaSensor(coordinator, device, description))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))
    _add_new_entities()


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
        self._uuid = _dev_uuid(device)
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
        if not super().available:
            return False
        if self.entity_description.source == "meta":
            return self._uuid in self.coordinator.data.get("meta", {})
        status = self.coordinator.data.get("statuses", {}).get(self._uuid)
        if not status:
            return False
        if status.get("has_data") is False:
            return False
        if self.entity_description.key in _MQTT_ONLY_SENSOR_KEYS:
            return self.entity_description.status_key in status
        return True

    @property
    def native_value(self) -> Any:
        desc = self.entity_description
        if desc.source == "meta":
            return self.coordinator.data.get("meta", {}).get(self._uuid, {}).get(desc.status_key)
        status = self.coordinator.data.get("statuses", {}).get(self._uuid, {})
        return status.get(desc.status_key)


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
