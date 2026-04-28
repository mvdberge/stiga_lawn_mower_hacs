"""STIGA binary sensor entities — sensor states, connectivity, and charging."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import StigaConfigEntry
from .const import DOMAIN
from .coordinator import StigaDataUpdateCoordinator

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class StigaBinarySensorDescription(BinarySensorEntityDescription):
    """Extended description for STIGA binary sensors.

    `source`:
      - "status"   — value comes from coordinator.data["statuses"][uuid]
      - "mqtt"     — value comes from coordinator.data["mqtt_connected"]
      - "settings" — value comes from coordinator.data["live_settings"][mac]

    `status_key`: key inside the selected source dict (ignored for "mqtt").
    `inverted`:   if True, the displayed state is the boolean complement of the raw value.
    """

    status_key: str = ""
    source: str = "status"
    inverted: bool = False


# ---------------------------------------------------------------------------
# Sensor derived from the `info_sensor` passthrough field in `statuses[uuid]`.
# When the mower raises an info_code that maps to a physical sensor, the
# `info_sensor` field holds the sensor name (e.g. "lift_sensor"). We compare
# against it to derive binary state.
# ---------------------------------------------------------------------------


def _info_sensor_value(status: dict, sensor_name: str) -> bool:
    """Return True when the mower's current info_sensor matches `sensor_name`."""
    return status.get("info_sensor") == sensor_name


BINARY_SENSOR_DESCRIPTIONS: tuple[StigaBinarySensorDescription, ...] = (
    # ---- Connectivity ----
    StigaBinarySensorDescription(
        key="mqtt_connected",
        translation_key="mqtt_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        source="mqtt",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ---- Status / safety sensors (derived from info_sensor passthrough) ----
    StigaBinarySensorDescription(
        key="rain_sensor",
        translation_key="rain_sensor",
        device_class=BinarySensorDeviceClass.MOISTURE,
        status_key="rain_sensor",
        source="info_sensor",
    ),
    StigaBinarySensorDescription(
        key="lift_sensor",
        translation_key="lift_sensor",
        device_class=BinarySensorDeviceClass.SAFETY,
        status_key="lift_sensor",
        source="info_sensor",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    StigaBinarySensorDescription(
        key="bump_sensor",
        translation_key="bump_sensor",
        device_class=BinarySensorDeviceClass.SAFETY,
        status_key="bump_sensor",
        source="info_sensor",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    StigaBinarySensorDescription(
        key="slope_sensor",
        translation_key="slope_sensor",
        device_class=BinarySensorDeviceClass.SAFETY,
        status_key="slope_sensor",
        source="info_sensor",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    StigaBinarySensorDescription(
        key="lid_sensor",
        translation_key="lid_sensor",
        device_class=BinarySensorDeviceClass.OPENING,
        status_key="lid_sensor",
        source="info_sensor",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # ---- Mower state ----
    StigaBinarySensorDescription(
        key="is_docked",
        translation_key="is_docked",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        status_key="is_docked",
        source="status",
    ),
    StigaBinarySensorDescription(
        key="battery_charging",
        translation_key="battery_charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        status_key="battery_charging",
        source="status",
    ),
    StigaBinarySensorDescription(
        key="error_active",
        translation_key="error_active",
        device_class=BinarySensorDeviceClass.PROBLEM,
        status_key="error_code",
        source="status",
        # `error_code` is non-None when an error is active.
        # We treat any truthy (non-zero, non-None) value as "problem present".
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities for all STIGA robots."""
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[StigaBinarySensor] = []
        for device in coordinator.data.get("devices", []):
            uuid = _dev_uuid(device)
            if not uuid:
                continue
            for description in BINARY_SENSOR_DESCRIPTIONS:
                key = (uuid, description.key)
                if key in known:
                    continue
                known.add(key)
                new_entities.append(StigaBinarySensor(coordinator, device, description))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))
    _add_new_entities()


class StigaBinarySensor(CoordinatorEntity[StigaDataUpdateCoordinator], BinarySensorEntity):
    """A single STIGA binary sensor entity."""

    _attr_has_entity_name = True
    entity_description: StigaBinarySensorDescription

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
        description: StigaBinarySensorDescription,
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
        if fw := a.get("firmware_version"):
            info["sw_version"] = fw
        if mac := a.get("mac_address"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        return info

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        desc = self.entity_description
        if desc.source == "mqtt":
            return True  # mqtt_connected is always known (default False)
        status = self.coordinator.data.get("statuses", {}).get(self._uuid)
        if not status:
            return False
        return status.get("has_data") is not False

    @property
    def is_on(self) -> bool | None:
        desc = self.entity_description
        if desc.source == "mqtt":
            return self.coordinator.data.get("mqtt_connected", False)

        status = self.coordinator.data.get("statuses", {}).get(self._uuid, {})

        if desc.source == "info_sensor":
            return _info_sensor_value(status, desc.status_key)

        # source == "status"
        raw = status.get(desc.status_key)
        if raw is None:
            return None
        if desc.key == "error_active":
            # Any non-zero error_code means problem is active.
            return bool(raw)
        return bool(raw)


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
