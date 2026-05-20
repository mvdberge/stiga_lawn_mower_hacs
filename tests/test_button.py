"""Tests for the StigaButton entity (stateless MQTT commands)."""

from __future__ import annotations

import pytest

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
    with pytest.raises(Exception, match="MQTT not connected"):
        await b.async_press()
