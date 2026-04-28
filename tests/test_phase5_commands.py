"""Tests for Phase 5 write-command entities and encode_settings_update."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.stiga_mower.button import (
    BUTTON_DESCRIPTIONS,
    StigaButton,
)
from custom_components.stiga_mower.coordinator import StigaDataUpdateCoordinator
from custom_components.stiga_mower.mqtt_messages import encode_settings_update
from custom_components.stiga_mower.number import NUMBER_DESCRIPTIONS, StigaNumber
from custom_components.stiga_mower.protobuf_codec import decode
from custom_components.stiga_mower.select import SELECT_DESCRIPTIONS, StigaSelect
from custom_components.stiga_mower.switch import SWITCH_DESCRIPTIONS, StigaSwitch

# ------------------------------------------------------------------ fixtures


def _make_coordinator(
    hass,
    *,
    live_settings=None,
    rest_status=None,
    mqtt_connected=True,
):
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)
    c._devices = [{"attributes": {"uuid": "u1", "name": "Bot", "mac_address": "MAC1"}}]
    if live_settings is not None:
        c._live_settings["MAC1"] = live_settings
    c.async_set_updated_data(c._build_data(rest_statuses={"u1": rest_status or {}}))

    mqtt = MagicMock()
    mqtt.connected = mqtt_connected
    mqtt.cmd_stop = AsyncMock()
    mqtt.cmd_go_home = AsyncMock()
    mqtt.cmd_settings_update = AsyncMock()
    mqtt.cmd_calibrate_blades = AsyncMock()
    mqtt.request_status = AsyncMock()
    c.mqtt = mqtt
    return c


def _device(coordinator):
    return coordinator.data["devices"][0]


def _number(coordinator, key="cutting_height"):
    desc = next(d for d in NUMBER_DESCRIPTIONS if d.key == key)
    return StigaNumber(coordinator, _device(coordinator), desc)


def _switch(coordinator, key):
    desc = next(d for d in SWITCH_DESCRIPTIONS if d.key == key)
    return StigaSwitch(coordinator, _device(coordinator), desc)


def _select(coordinator, key):
    desc = next(d for d in SELECT_DESCRIPTIONS if d.key == key)
    return StigaSelect(coordinator, _device(coordinator), desc)


def _button(coordinator, key):
    desc = next(d for d in BUTTON_DESCRIPTIONS if d.key == key)
    return StigaButton(coordinator, _device(coordinator), desc)


# ------------------------------------------------------------------ encode_settings_update


def test_encode_settings_update_rain_sensor_enabled() -> None:
    payload = encode_settings_update({"rain_sensor_enabled": True})
    decoded = decode(payload)
    # Field 1 = cmd_id (18), field 2 = params, field 3 = echo
    assert decoded[1] == 18
    params = decoded[2]
    assert isinstance(params, dict)
    assert params[1][1] == 1  # rain.enabled = True


def test_encode_settings_update_cutting_height_40mm() -> None:
    payload = encode_settings_update({"cutting_height_mm": 40})
    decoded = decode(payload)
    params = decoded[2]
    # 40mm -> index 4
    assert params[4][2] == 4


def test_encode_settings_update_anti_theft() -> None:
    payload = encode_settings_update({"anti_theft": False})
    decoded = decode(payload)
    params = decoded[2]
    assert params[6] == 0


def test_encode_settings_update_rain_delay_8h() -> None:
    payload = encode_settings_update({"rain_sensor_delay_h": 8})
    decoded = decode(payload)
    params = decoded[2]
    # 8h -> index 1
    assert params[1][2] == 1


def test_encode_settings_update_unknown_cutting_height_skipped() -> None:
    # 37mm is not a valid height — should not include cutting field
    payload = encode_settings_update({"cutting_height_mm": 37})
    decoded = decode(payload)
    params = decoded.get(2)
    # params may be None or not contain field 4
    if params is not None:
        assert 4 not in params


def test_encode_settings_update_multiple_fields() -> None:
    payload = encode_settings_update(
        {
            "rain_sensor_enabled": True,
            "keyboard_lock": False,
            "cutting_height_mm": 30,
        }
    )
    decoded = decode(payload)
    params = decoded[2]
    assert params[1][1] == 1  # rain on
    assert params[2] == 0  # keyboard_lock off
    assert params[4][2] == 2  # 30mm -> index 2


# ------------------------------------------------------------------ number: cutting_height


def test_number_reads_from_live_settings(hass) -> None:
    c = _make_coordinator(hass, live_settings={"cutting_height_mm": 40})
    n = _number(c)
    assert n.native_value == 40.0
    assert n.available is True


def test_number_reads_from_rest_status_fallback(hass) -> None:
    c = _make_coordinator(hass, rest_status={"cutting_height_mm": 35, "has_data": True})
    n = _number(c)
    assert n.native_value == 35.0


def test_number_unavailable_when_no_value(hass) -> None:
    c = _make_coordinator(hass)
    n = _number(c)
    assert n.available is False


@pytest.mark.asyncio
async def test_number_set_value_calls_mqtt(hass) -> None:
    c = _make_coordinator(hass, live_settings={"cutting_height_mm": 40})
    n = _number(c)
    await n.async_set_native_value(45)
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"cutting_height_mm": 45})


@pytest.mark.asyncio
async def test_number_raises_when_mqtt_disconnected(hass) -> None:
    c = _make_coordinator(hass, live_settings={"cutting_height_mm": 40}, mqtt_connected=False)
    n = _number(c)
    with pytest.raises(Exception, match="MQTT not connected"):
        await n.async_set_native_value(45)


# ------------------------------------------------------------------ switch


@pytest.mark.asyncio
async def test_switch_turn_on_calls_mqtt(hass) -> None:
    c = _make_coordinator(hass, live_settings={"rain_sensor_enabled": False})
    s = _switch(c, "rain_sensor_enabled")
    assert s.is_on is False
    await s.async_turn_on()
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"rain_sensor_enabled": True})


@pytest.mark.asyncio
async def test_switch_turn_off_calls_mqtt(hass) -> None:
    c = _make_coordinator(hass, live_settings={"rain_sensor_enabled": True})
    s = _switch(c, "rain_sensor_enabled")
    assert s.is_on is True
    await s.async_turn_off()
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"rain_sensor_enabled": False})


def test_switch_unavailable_when_no_live_settings(hass) -> None:
    c = _make_coordinator(hass)
    s = _switch(c, "rain_sensor_enabled")
    assert s.available is False


@pytest.mark.asyncio
async def test_switch_raises_when_mqtt_disconnected(hass) -> None:
    c = _make_coordinator(hass, live_settings={"rain_sensor_enabled": True}, mqtt_connected=False)
    s = _switch(c, "rain_sensor_enabled")
    with pytest.raises(Exception, match="MQTT not connected"):
        await s.async_turn_off()


# ------------------------------------------------------------------ select


def test_select_current_option_from_live_settings(hass) -> None:
    c = _make_coordinator(hass, live_settings={"cutting_height_mm": 0, "cutting_mode": 1})
    s = _select(c, "cutting_mode")
    assert s.current_option == "chessBoard"


def test_select_rain_delay_current_option(hass) -> None:
    # live_settings stores decoded hours directly (decode_settings maps index->hours)
    c = _make_coordinator(hass, live_settings={"rain_sensor_delay_h": 8})
    s = _select(c, "rain_sensor_delay")
    assert s.current_option == "8"


def test_select_unavailable_when_no_live_settings(hass) -> None:
    c = _make_coordinator(hass)
    s = _select(c, "cutting_mode")
    assert s.available is False


@pytest.mark.asyncio
async def test_select_sends_correct_wire_value(hass) -> None:
    c = _make_coordinator(hass, live_settings={"cutting_mode": 0})
    s = _select(c, "cutting_mode")
    await s.async_select_option("northSouth")
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"cutting_mode": 5})


@pytest.mark.asyncio
async def test_select_raises_on_unknown_option(hass) -> None:
    c = _make_coordinator(hass, live_settings={"cutting_mode": 0})
    s = _select(c, "cutting_mode")
    with pytest.raises(Exception, match="Unknown option"):
        await s.async_select_option("spirograph")


# ------------------------------------------------------------------ button


@pytest.mark.asyncio
async def test_button_calibrate_blades(hass) -> None:
    c = _make_coordinator(hass)
    b = _button(c, "calibrate_blades")
    await b.async_press()
    c.mqtt.cmd_calibrate_blades.assert_awaited_once_with("MAC1")


@pytest.mark.asyncio
async def test_button_refresh_status(hass) -> None:
    c = _make_coordinator(hass)
    b = _button(c, "refresh_status")
    await b.async_press()
    c.mqtt.request_status.assert_awaited_once_with("MAC1")


@pytest.mark.asyncio
async def test_button_raises_when_mqtt_disconnected(hass) -> None:
    c = _make_coordinator(hass, mqtt_connected=False)
    b = _button(c, "refresh_status")
    with pytest.raises(Exception, match="MQTT not connected"):
        await b.async_press()


# ------------------------------------------------------------------ lawn_mower PAUSE


@pytest.mark.asyncio
async def test_lawn_mower_pause_uses_mqtt_stop(hass) -> None:
    from custom_components.stiga_mower.lawn_mower import StigaLawnMower

    c = _make_coordinator(hass, rest_status={"has_data": True, "current_action": "MOWING"})
    mower = StigaLawnMower(c, _device(c))
    await mower.async_pause()
    c.mqtt.cmd_stop.assert_awaited_once_with("MAC1")


@pytest.mark.asyncio
async def test_lawn_mower_dock_uses_mqtt_go_home(hass) -> None:
    from custom_components.stiga_mower.lawn_mower import StigaLawnMower

    c = _make_coordinator(hass, rest_status={"has_data": True, "current_action": "MOWING"})
    mower = StigaLawnMower(c, _device(c))
    await mower.async_dock()
    c.mqtt.cmd_go_home.assert_awaited_once_with("MAC1")


@pytest.mark.asyncio
async def test_lawn_mower_dock_falls_back_to_rest_when_mqtt_off(hass) -> None:
    from custom_components.stiga_mower.lawn_mower import StigaLawnMower

    c = _make_coordinator(
        hass,
        rest_status={"has_data": True},
        mqtt_connected=False,
    )
    c.api.stop_mowing = AsyncMock()
    mower = StigaLawnMower(c, _device(c))
    await mower.async_dock()
    c.api.stop_mowing.assert_awaited_once_with("u1")
    c.mqtt.cmd_go_home.assert_not_awaited()
