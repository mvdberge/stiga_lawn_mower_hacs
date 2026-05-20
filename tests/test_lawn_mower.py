"""Tests for the StigaLawnMower entity (pause / dock action paths)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.stiga_mower.lawn_mower import StigaLawnMower

from ._entity_helpers import device, make_coordinator


@pytest.mark.asyncio
async def test_lawn_mower_pause_uses_mqtt_stop(hass) -> None:
    c = make_coordinator(hass, rest_status={"has_data": True, "current_action": "MOWING"})
    mower = StigaLawnMower(c, device(c))
    await mower.async_pause()
    c.mqtt.cmd_stop.assert_awaited_once_with("MAC1")


@pytest.mark.asyncio
async def test_lawn_mower_dock_uses_mqtt_go_home(hass) -> None:
    c = make_coordinator(hass, rest_status={"has_data": True, "current_action": "MOWING"})
    mower = StigaLawnMower(c, device(c))
    await mower.async_dock()
    c.mqtt.cmd_go_home.assert_awaited_once_with("MAC1")


@pytest.mark.asyncio
async def test_lawn_mower_dock_falls_back_to_rest_when_mqtt_off(hass) -> None:
    c = make_coordinator(
        hass,
        rest_status={"has_data": True},
        mqtt_connected=False,
    )
    c.api.stop_mowing = AsyncMock()
    mower = StigaLawnMower(c, device(c))
    await mower.async_dock()
    c.api.stop_mowing.assert_awaited_once_with("u1")
    c.mqtt.cmd_go_home.assert_not_awaited()
