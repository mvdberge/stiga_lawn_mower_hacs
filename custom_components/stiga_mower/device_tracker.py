"""STIGA device tracker — live GPS position from MQTT ROBOT_POSITION frames.

The mower reports lat/lon offsets in centimetres relative to the base station.
We convert those to absolute WGS84 coordinates using the base station's
position from the REST garage payload (`last_position`).

If neither the base-station position nor an MQTT position frame is available,
the entity stays unavailable rather than emitting a stale or wrong location.
"""

from __future__ import annotations

import math

from homeassistant.components.device_tracker import (
    TrackerEntity,
    TrackerEntityDescription,
)
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import StigaConfigEntry
from .const import DOMAIN, split_firmware_version
from .coordinator import StigaDataUpdateCoordinator

PARALLEL_UPDATES = 1

# 1 degree latitude ≈ 111 111 m.
_M_PER_DEG_LAT = 111_111.0


def _offset_to_wgs84(
    base_lat: float,
    base_lon: float,
    lat_offset_m: float,
    lon_offset_m: float,
) -> tuple[float, float]:
    """Convert (lat_offset_m, lon_offset_m) relative to (base_lat, base_lon)."""
    d_lat = lat_offset_m / _M_PER_DEG_LAT
    # 1° longitude shrinks with cos(lat)
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(base_lat))
    d_lon = lon_offset_m / m_per_deg_lon if m_per_deg_lon else 0.0
    return base_lat + d_lat, base_lon + d_lon


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up device tracker entities for all STIGA robots."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[StigaPositionTracker] = []
        for device in coordinator.data.get("devices", []):
            uuid = _dev_uuid(device)
            if not uuid or uuid in known:
                continue
            known.add(uuid)
            new_entities.append(StigaPositionTracker(coordinator, device))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))
    _add_new_entities()


class StigaPositionTracker(CoordinatorEntity[StigaDataUpdateCoordinator], TrackerEntity):
    """GPS position tracker for a STIGA robot mower."""

    _attr_has_entity_name = True
    _attr_translation_key = "position"
    _attr_source_type = SourceType.GPS
    # Default off — only useful when the user is actively tracking the mower.
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    entity_description = TrackerEntityDescription(key="position")

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        attrs = device.get("attributes") or {}
        self._uuid = attrs.get("uuid", "")
        self._mac = attrs.get("mac_address", "")
        self._attr_unique_id = f"stiga_{self._uuid}_position"

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

    def _gps_offsets(self) -> tuple[float, float] | None:
        """Return (lat_offset_m, lon_offset_m) from the latest ROBOT_POSITION frame, or None."""
        pos = self.coordinator.data.get("live_position", {}).get(self._mac)
        if not pos:
            return None
        lat_m = pos.get("lat_offset_m")
        lon_m = pos.get("lon_offset_m")
        if lat_m is None or lon_m is None:
            return None
        return lat_m, lon_m

    def _base_position(self) -> tuple[float, float] | None:
        """Return (lat, lon) of the base station from REST garage data."""
        attrs = self._device_attrs()
        last_pos = attrs.get("last_position")
        if not isinstance(last_pos, dict):
            return None
        lat = last_pos.get("lat") or last_pos.get("latitude")
        lon = last_pos.get("lon") or last_pos.get("longitude")
        if lat is None or lon is None:
            return None
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        if not self.coordinator.data:
            return False
        return self._gps_offsets() is not None

    @property
    def latitude(self) -> float | None:
        offsets = self._gps_offsets()
        if offsets is None:
            return None
        base = self._base_position()
        if base is None:
            return None
        lat, _ = _offset_to_wgs84(base[0], base[1], offsets[0], offsets[1])
        return round(lat, 7)

    @property
    def longitude(self) -> float | None:
        offsets = self._gps_offsets()
        if offsets is None:
            return None
        base = self._base_position()
        if base is None:
            return None
        _, lon = _offset_to_wgs84(base[0], base[1], offsets[0], offsets[1])
        return round(lon, 7)


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
