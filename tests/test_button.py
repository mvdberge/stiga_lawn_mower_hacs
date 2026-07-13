"""Tests for the StigaButton entity (stateless MQTT commands)."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError

from ._entity_helpers import button, make_coordinator


@pytest.mark.asyncio
async def test_button_calibrate_blades(hass) -> None:
    c = make_coordinator(hass)
    b = button(c, "calibrate_blades")
    await b.async_press()
    c.mqtt.cmd_calibrate_blades.assert_awaited_once_with("MAC1")


@pytest.mark.asyncio
async def test_button_reset_error(hass) -> None:
    c = make_coordinator(hass)
    b = button(c, "reset_error")
    await b.async_press()
    c.mqtt.cmd_reset_error.assert_awaited_once_with("MAC1")


@pytest.mark.asyncio
async def test_button_raises_when_mqtt_disconnected(hass) -> None:
    c = make_coordinator(hass, mqtt_connected=False)
    b = button(c, "reset_error")
    with pytest.raises(HomeAssistantError) as err:
        await b.async_press()
    assert err.value.translation_key == "mqtt_not_connected"


@pytest.mark.asyncio
async def test_button_perform_boot(hass) -> None:
    c = make_coordinator(hass, rest_status={"current_action": "STARTUP_REQUIRED"})
    b = button(c, "perform_boot")
    await b.async_press()
    c.mqtt.cmd_boot.assert_awaited_once_with("MAC1")


def test_button_perform_boot_available_only_in_startup(hass) -> None:
    # Available only while the mower reports STARTUP_REQUIRED.
    c = make_coordinator(hass, rest_status={"current_action": "STARTUP_REQUIRED"})
    assert button(c, "perform_boot").available is True

    c = make_coordinator(hass, rest_status={"current_action": "DOCKED"})
    assert button(c, "perform_boot").available is False

    # No status at all → not available.
    c = make_coordinator(hass)
    assert button(c, "perform_boot").available is False


def test_button_without_status_gate_always_available(hass) -> None:
    # Buttons without available_status_types stay available regardless of state.
    c = make_coordinator(hass)
    assert button(c, "reset_error").available is True
