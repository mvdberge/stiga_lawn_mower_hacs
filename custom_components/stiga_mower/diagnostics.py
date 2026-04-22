"""Diagnostics support for the STIGA lawn mower integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import StigaConfigEntry
from .const import CONF_EMAIL, CONF_PASSWORD

REDACT_ENTRY_DATA = {CONF_EMAIL, CONF_PASSWORD}
REDACT_DEVICE_FIELDS = {"serial_number", "uuid", "name"}


def _redact_devices(devices: list[dict]) -> list[dict]:
    redacted: list[dict] = []
    for device in devices:
        attrs = dict(device.get("attributes") or {})
        for field in REDACT_DEVICE_FIELDS:
            if field in attrs:
                attrs[field] = "**REDACTED**"
        redacted.append({**device, "attributes": attrs})
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: StigaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), REDACT_ENTRY_DATA),
            "unique_id": "**REDACTED**" if entry.unique_id else None,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
        },
        "devices": _redact_devices(data.get("devices", [])),
        "statuses": {
            "**REDACTED**": status
            for status in data.get("statuses", {}).values()
        },
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: StigaConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single device."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}

    device_uuid = next(
        (ident[1] for ident in device.identifiers if ident[0] == entry.domain),
        None,
    )
    if device_uuid is None:
        return {"error": "device_not_found"}

    matched = next(
        (
            d
            for d in data.get("devices", [])
            if (d.get("attributes") or {}).get("uuid") == device_uuid
        ),
        None,
    )
    status = data.get("statuses", {}).get(device_uuid, {})

    return {
        "device": _redact_devices([matched])[0] if matched else None,
        "status": status,
    }
