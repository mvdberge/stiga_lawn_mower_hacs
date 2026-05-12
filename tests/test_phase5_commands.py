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
from custom_components.stiga_mower.select import (
    SELECT_DESCRIPTIONS,
    StigaScheduleModeSelect,
    StigaSelect,
)
from custom_components.stiga_mower.switch import SWITCH_DESCRIPTIONS, StigaSwitch

# ------------------------------------------------------------------ fixtures


def _make_coordinator(
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


@pytest.mark.asyncio
async def test_switch_optimistic_update_after_turn_off(hass) -> None:
    """After turn_off the switch must show False even if the firmware's SETTINGS
    response omits rain_sensor_enabled (proto3 default = False → absent).
    The coordinator merge would leave live_settings stale without the
    optimistic apply_live_settings call that _send now performs.
    """
    c = _make_coordinator(hass, live_settings={"rain_sensor_enabled": True})
    s = _switch(c, "rain_sensor_enabled")
    assert s.is_on is True
    await s.async_turn_off()
    # State must reflect False immediately — before any SETTINGS frame arrives.
    assert s.is_on is False


@pytest.mark.asyncio
async def test_switch_optimistic_update_after_turn_on(hass) -> None:
    c = _make_coordinator(hass, live_settings={"rain_sensor_enabled": False})
    s = _switch(c, "rain_sensor_enabled")
    await s.async_turn_on()
    assert s.is_on is True


def test_switch_unavailable_when_no_live_settings(hass) -> None:
    c = _make_coordinator(hass)
    s = _switch(c, "rain_sensor_enabled")
    assert s.available is False


def test_switch_available_with_missing_key_defaults_to_false(hass) -> None:
    """Proto3 default-omission: a SETTINGS frame omits bool fields that are
    currently False. As soon as any SETTINGS frame arrives, every switch must
    be available — missing keys mean ``False``, not ``unknown``.
    """
    c = _make_coordinator(hass, live_settings={"rain_sensor_enabled": True})
    s = _switch(c, "anti_theft")
    assert s.available is True
    assert s.is_on is False


@pytest.mark.asyncio
async def test_switch_rain_enable_includes_delay_and_zch(hass) -> None:
    """Activating rain sensor: delay + zone_cutting_height_enabled are appended.

    Verified against 2026-05-12 app capture: SETTINGS_UPDATE params =
    {1: {1:1, 2:1}, 4: {1:1}} when enabling at 8 h with zch=True.
    """
    c = _make_coordinator(
        hass,
        live_settings={
            "rain_sensor_enabled": False,
            "rain_sensor_delay_h": 8,
            "zone_cutting_height_enabled": True,
        },
    )
    s = _switch(c, "rain_sensor_enabled")
    await s.async_turn_on()
    c.mqtt.cmd_settings_update.assert_awaited_once_with(
        "MAC1",
        {
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 8,
            "zone_cutting_height_enabled": True,
        },
    )


@pytest.mark.asyncio
async def test_switch_rain_disable_includes_zch_not_delay(hass) -> None:
    """Deactivating rain sensor: only enabled flag + zch sent (no delay).

    Verified against 2026-05-12 app capture: params = {1: {1:0}, 4: {1:1}}.
    """
    c = _make_coordinator(
        hass,
        live_settings={
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 12,
            "zone_cutting_height_enabled": True,
        },
    )
    s = _switch(c, "rain_sensor_enabled")
    await s.async_turn_off()
    c.mqtt.cmd_settings_update.assert_awaited_once_with(
        "MAC1",
        {
            "rain_sensor_enabled": False,
            "zone_cutting_height_enabled": True,
        },
    )


@pytest.mark.asyncio
async def test_switch_rain_enable_without_delay_in_live_settings(hass) -> None:
    """Enabling rain sensor when live_settings has no delay: omit delay gracefully."""
    c = _make_coordinator(hass, live_settings={"rain_sensor_enabled": False})
    s = _switch(c, "rain_sensor_enabled")
    await s.async_turn_on()
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"rain_sensor_enabled": True})


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
    assert s.current_option == "chess_board"


def test_select_rain_delay_current_option(hass) -> None:
    # live_settings stores decoded hours directly (decode_settings maps index->hours)
    c = _make_coordinator(hass, live_settings={"rain_sensor_delay_h": 8})
    s = _select(c, "rain_sensor_delay")
    assert s.current_option == "8"


def test_select_unavailable_when_no_live_settings(hass) -> None:
    c = _make_coordinator(hass)
    s = _select(c, "cutting_mode")
    assert s.available is False


def test_select_rain_delay_defaults_to_4h_when_key_missing(hass) -> None:
    """Proto3 default-omission: SETTINGS frames omit rain_sensor_delay_h when
    the wire index is 0 (= 4 hours). The select must still be available and
    report ``"4"`` instead of staying permanently ``unavailable``.
    """
    c = _make_coordinator(hass, live_settings={"rain_sensor_enabled": True})
    s = _select(c, "rain_sensor_delay")
    assert s.available is True
    assert s.current_option == "4"


def test_select_unavailable_when_mqtt_disconnected(hass) -> None:
    c = _make_coordinator(hass, live_settings={"rain_sensor_delay_h": 8}, mqtt_connected=False)
    s = _select(c, "rain_sensor_delay")
    assert s.available is False


# -------------------------------------------------------- schedule_mode select


def _schedule_mode_select(coordinator) -> StigaScheduleModeSelect:
    return StigaScheduleModeSelect(coordinator, _device(coordinator))


def test_schedule_mode_options_are_manual_and_auto(hass) -> None:
    c = _make_coordinator(hass, live_schedule={"enabled": True})
    s = _schedule_mode_select(c)
    assert s.options == ["manual", "auto"]


def test_schedule_mode_current_option_auto_when_enabled(hass) -> None:
    c = _make_coordinator(hass, live_schedule={"enabled": True})
    s = _schedule_mode_select(c)
    assert s.available is True
    assert s.current_option == "auto"


def test_schedule_mode_current_option_manual_when_disabled(hass) -> None:
    c = _make_coordinator(hass, live_schedule={"enabled": False})
    s = _schedule_mode_select(c)
    assert s.available is True
    assert s.current_option == "manual"


def test_schedule_mode_defaults_to_manual_when_key_missing(hass) -> None:
    """Proto3 default-omission: a disabled schedule (field 1 = False) is
    omitted on the wire. A populated live_schedule entry without the key
    therefore means "manual", not "unavailable".
    """
    c = _make_coordinator(hass, live_schedule={"days": []})
    s = _schedule_mode_select(c)
    assert s.available is True
    assert s.current_option == "manual"


def test_schedule_mode_unavailable_without_live_schedule(hass) -> None:
    c = _make_coordinator(hass)
    s = _schedule_mode_select(c)
    assert s.available is False
    assert s.current_option is None


def test_schedule_mode_unavailable_when_mqtt_disconnected(hass) -> None:
    c = _make_coordinator(hass, live_schedule={"enabled": True}, mqtt_connected=False)
    s = _schedule_mode_select(c)
    assert s.available is False


def test_schedule_mode_has_no_entity_category(hass) -> None:
    """The schedule mode is a user-facing operating control and must live in
    the default "Controls" HA category — not in "Configuration".
    """
    c = _make_coordinator(hass, live_schedule={"enabled": True})
    s = _schedule_mode_select(c)
    assert s.entity_category is None


@pytest.mark.asyncio
async def test_schedule_mode_select_auto_sends_true(hass) -> None:
    c = _make_coordinator(hass, live_schedule={"enabled": False})
    s = _schedule_mode_select(c)
    await s.async_select_option("auto")
    c.mqtt.cmd_schedule_set_enabled.assert_awaited_once_with("MAC1", True)


@pytest.mark.asyncio
async def test_schedule_mode_select_manual_sends_false(hass) -> None:
    c = _make_coordinator(hass, live_schedule={"enabled": True})
    s = _schedule_mode_select(c)
    await s.async_select_option("manual")
    c.mqtt.cmd_schedule_set_enabled.assert_awaited_once_with("MAC1", False)


@pytest.mark.asyncio
async def test_schedule_mode_unknown_option_raises(hass) -> None:
    c = _make_coordinator(hass, live_schedule={"enabled": True})
    s = _schedule_mode_select(c)
    with pytest.raises(Exception, match="Unknown option"):
        await s.async_select_option("disco")


@pytest.mark.asyncio
async def test_schedule_mode_raises_when_mqtt_disconnected(hass) -> None:
    c = _make_coordinator(hass, live_schedule={"enabled": True}, mqtt_connected=False)
    s = _schedule_mode_select(c)
    with pytest.raises(Exception, match="MQTT not connected"):
        await s.async_select_option("auto")


@pytest.mark.asyncio
async def test_select_rain_delay_includes_enabled_and_zch(hass) -> None:
    """Changing rain delay: enabled flag + zch are appended from live_settings.

    Verified against 2026-05-12 app capture: selecting 12 h sends
    params = {1: {1:1, 2:2}, 4: {1:1}}.
    """
    c = _make_coordinator(
        hass,
        live_settings={
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 4,
            "zone_cutting_height_enabled": True,
        },
    )
    s = _select(c, "rain_sensor_delay")
    await s.async_select_option("12")
    c.mqtt.cmd_settings_update.assert_awaited_once_with(
        "MAC1",
        {
            "rain_sensor_delay_h": 12,
            "rain_sensor_enabled": True,
            "zone_cutting_height_enabled": True,
        },
    )


@pytest.mark.asyncio
async def test_select_rain_delay_without_enabled_in_live_settings(hass) -> None:
    """Changing delay when live_settings has no enabled flag: omit gracefully."""
    c = _make_coordinator(hass, live_settings={"rain_sensor_delay_h": 4})
    s = _select(c, "rain_sensor_delay")
    await s.async_select_option("8")
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"rain_sensor_delay_h": 8})


@pytest.mark.asyncio
async def test_select_rain_delay_optimistic_update_to_4h(hass) -> None:
    """Selecting 4 h must show immediately even if the firmware omits the field.

    The wire index for 4 h is 0 (proto3 default), so the firmware's SETTINGS
    response omits rain[2]. The coordinator merge cannot detect the change;
    the optimistic apply_live_settings in async_select_option must apply it.
    """
    c = _make_coordinator(
        hass,
        live_settings={
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 8,
            "zone_cutting_height_enabled": True,
        },
    )
    s = _select(c, "rain_sensor_delay")
    assert s.current_option == "8"
    await s.async_select_option("4")
    assert s.current_option == "4"


@pytest.mark.asyncio
async def test_select_sends_correct_wire_value(hass) -> None:
    c = _make_coordinator(hass, live_settings={"cutting_mode": 0})
    s = _select(c, "cutting_mode")
    await s.async_select_option("north_south")
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
async def test_button_reset_error(hass) -> None:
    c = _make_coordinator(hass)
    b = _button(c, "reset_error")
    await b.async_press()
    c.mqtt.cmd_reset_error.assert_awaited_once_with("MAC1")


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
