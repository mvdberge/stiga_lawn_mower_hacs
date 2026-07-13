"""Diagnostics support for the STIGA lawn mower integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import StigaConfigEntry
from .const import CONF_EMAIL, CONF_PASSWORD

REDACT_ENTRY_DATA = {CONF_EMAIL, CONF_PASSWORD}
# The /api/garage device `attributes` dict carries a broad set of hardware and
# account identifiers. Redact every one that can identify the device, its owner,
# the SIM, the broker or the mower's physical location — not just the name/uuid.
REDACT_DEVICE_FIELDS = {
    "serial_number",
    "uuid",
    "name",
    "mac_address",
    "base_uuid",
    "sim_uuid",
    "device_detail_uuid",
    "store_uuid",
    "perimeter_uuid",
    "buyer_uuid",
    "country_uuid",
    "magento_registration_id",
    "broker_id",
    "last_position",
}
REDACT_BASE_FIELDS = {"serial_number", "uuid", "mac_address"}


def _redact_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for device in devices:
        attrs = dict(device.get("attributes") or {})
        for field in REDACT_DEVICE_FIELDS:
            if field in attrs:
                attrs[field] = "**REDACTED**"
        redacted.append({**device, "attributes": attrs})
    return redacted


def _redact_bases(bases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact PII-like fields on base-station records (REST snapshot)."""
    redacted: list[dict[str, Any]] = []
    for base in bases:
        copy = dict(base)
        for field in REDACT_BASE_FIELDS:
            if field in copy and copy[field] is not None:
                copy[field] = "**REDACTED**"
        redacted.append(copy)
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
        # Drop the UUID keys (PII) but keep every device's status dict.
        # The previous {"**REDACTED**": status for ...} comprehension
        # collapsed to a single entry — keeping a list preserves the
        # full picture without leaking identifiers.
        "statuses": list(data.get("statuses", {}).values()),
        # Base-station REST snapshot + live MQTT decodes. The MAC keys in
        # live_base_* dicts are dropped (PII) — the inner dicts carry every
        # decoded field already, so the keying is redundant for diagnostics.
        "bases": _redact_bases(data.get("bases", [])),
        "live_base_status": list(data.get("live_base_status", {}).values()),
        "live_base_version": list(data.get("live_base_version", {}).values()),
        # Live MQTT state: connection flag plus the decoded meta/settings/
        # schedule buckets. The uuid/MAC keys are dropped (PII) — the inner
        # dicts carry every decoded field already, matching the handling of
        # "statuses" and "live_base_*" above. None of these buckets contain
        # credentials.
        "mqtt_connected": data.get("mqtt_connected"),
        "meta": list(data.get("meta", {}).values()),
        "live_settings": list(data.get("live_settings", {}).values()),
        "live_schedule": list(data.get("live_schedule", {}).values()),
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
