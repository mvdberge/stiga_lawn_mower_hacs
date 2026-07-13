"""Shared entity-construction helpers for platform-level tests.

Underscore-prefixed module so pytest does not collect it as a test file. Each
helper builds a single coordinator-backed entity wired against a mocked MQTT
client, which is the unit under test for almost every write-path assertion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.stiga_mower.button import BUTTON_DESCRIPTIONS, StigaButton
from custom_components.stiga_mower.coordinator import StigaDataUpdateCoordinator
from custom_components.stiga_mower.number import NUMBER_DESCRIPTIONS, StigaNumber
from custom_components.stiga_mower.select import (
    SELECT_DESCRIPTIONS,
    StigaScheduleModeSelect,
    StigaSelect,
)
from custom_components.stiga_mower.switch import SWITCH_DESCRIPTIONS, StigaSwitch


def make_coordinator(
    hass,
    *,
    live_settings=None,
    live_schedule=None,
    rest_status=None,
    mqtt_connected=True,
):
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    entry.async_create_background_task = lambda hass, coro, name=None: hass.async_create_task(coro)
    c = StigaDataUpdateCoordinator(hass, entry, api)
    c._devices = [{"attributes": {"uuid": "u1", "name": "Bot", "mac_address": "MAC1"}}]
    if live_settings is not None:
        c._live_settings["MAC1"] = live_settings
    if live_schedule is not None:
        c._live_schedule["MAC1"] = live_schedule
    c.async_set_updated_data(c._build_data(rest_statuses={"u1": rest_status or {}}))

    mqtt = MagicMock()
    mqtt.connected = mqtt_connected
    mqtt.cmd_stop = AsyncMock()
    mqtt.cmd_go_home = AsyncMock()
    mqtt.cmd_settings_update = AsyncMock()
    mqtt.cmd_calibrate_blades = AsyncMock()
    mqtt.cmd_reset_error = AsyncMock()
    mqtt.cmd_schedule_set_enabled = AsyncMock()
    mqtt.request_status = AsyncMock()
    c.mqtt = mqtt
    return c


def device(coordinator):
    return coordinator.data["devices"][0]


def number(coordinator, key="cutting_height"):
    desc = next(d for d in NUMBER_DESCRIPTIONS if d.key == key)
    return StigaNumber(coordinator, device(coordinator), desc)


def switch(coordinator, key):
    desc = next(d for d in SWITCH_DESCRIPTIONS if d.key == key)
    return StigaSwitch(coordinator, device(coordinator), desc)


def select(coordinator, key):
    desc = next(d for d in SELECT_DESCRIPTIONS if d.key == key)
    return StigaSelect(coordinator, device(coordinator), desc)


def button(coordinator, key):
    desc = next(d for d in BUTTON_DESCRIPTIONS if d.key == key)
    return StigaButton(coordinator, device(coordinator), desc)


def schedule_mode_select(coordinator) -> StigaScheduleModeSelect:
    return StigaScheduleModeSelect(coordinator, device(coordinator))
