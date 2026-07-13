"""Tests for diagnostics redaction — the privacy-critical path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.stiga_mower.coordinator import StigaDataUpdateCoordinator
from custom_components.stiga_mower.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)

# Sensitive attributes that a real /api/garage device record carries and that
# must never leak into a diagnostics dump.
_SENSITIVE = {
    "serial_number": "SN12345",
    "uuid": "dev-uuid",
    "name": "Back Garden",
    "mac_address": "3C:22:7F:AA:BA:EA",
    "base_uuid": "base-uuid",
    "sim_uuid": "sim-uuid",
    "broker_id": "broker-42",
    "last_position": {"lat": 51.1, "lon": 6.2},
    "magento_registration_id": "magento-1",
}


def _coordinator(hass):
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    entry.async_create_background_task = lambda hass, coro, name=None: hass.async_create_task(coro)
    c = StigaDataUpdateCoordinator(hass, entry, api)
    c._devices = [{"attributes": {**_SENSITIVE}}]
    c.async_set_updated_data(c._build_data(rest_statuses={"dev-uuid": {"has_data": True}}))
    return c


def _entry(coordinator):
    entry = MagicMock()
    entry.runtime_data = coordinator
    entry.data = {"email": "user@example.com", "password": "secret"}
    entry.unique_id = "user@example.com"
    entry.domain = "stiga_mower"
    return entry


@pytest.mark.asyncio
async def test_config_entry_diagnostics_redacts_all_sensitive_fields(hass) -> None:
    c = _coordinator(hass)
    diag = await async_get_config_entry_diagnostics(hass, _entry(c))

    attrs = diag["devices"][0]["attributes"]
    for field in _SENSITIVE:
        assert attrs[field] == "**REDACTED**", f"{field} leaked: {attrs[field]!r}"
    # Credentials in the entry data are redacted too.
    assert diag["entry"]["data"]["password"] == "**REDACTED**"
    assert diag["entry"]["data"]["email"] == "**REDACTED**"


@pytest.mark.asyncio
async def test_device_diagnostics_redacts_sensitive_fields(hass) -> None:
    c = _coordinator(hass)
    entry = _entry(c)
    device = MagicMock()
    device.identifiers = {("stiga_mower", "dev-uuid")}

    diag = await async_get_device_diagnostics(hass, entry, device)

    attrs = diag["device"]["attributes"]
    assert attrs["mac_address"] == "**REDACTED**"
    assert attrs["broker_id"] == "**REDACTED**"
    assert attrs["last_position"] == "**REDACTED**"
