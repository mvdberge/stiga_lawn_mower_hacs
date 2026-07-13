"""Tests for the StigaSwitch entity (boolean MQTT settings)."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError

from ._entity_helpers import make_coordinator, switch


@pytest.mark.asyncio
async def test_switch_turn_on_calls_mqtt(hass) -> None:
    c = make_coordinator(hass, live_settings={"rain_sensor_enabled": False})
    s = switch(c, "rain_sensor_enabled")
    assert s.is_on is False
    await s.async_turn_on()
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"rain_sensor_enabled": True})


@pytest.mark.asyncio
async def test_switch_turn_off_calls_mqtt(hass) -> None:
    c = make_coordinator(hass, live_settings={"rain_sensor_enabled": True})
    s = switch(c, "rain_sensor_enabled")
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
    c = make_coordinator(hass, live_settings={"rain_sensor_enabled": True})
    s = switch(c, "rain_sensor_enabled")
    assert s.is_on is True
    await s.async_turn_off()
    # State must reflect False immediately — before any SETTINGS frame arrives.
    assert s.is_on is False


@pytest.mark.asyncio
async def test_switch_optimistic_update_after_turn_on(hass) -> None:
    c = make_coordinator(hass, live_settings={"rain_sensor_enabled": False})
    s = switch(c, "rain_sensor_enabled")
    await s.async_turn_on()
    assert s.is_on is True


def test_switch_unavailable_when_no_live_settings(hass) -> None:
    c = make_coordinator(hass)
    s = switch(c, "rain_sensor_enabled")
    assert s.available is False


def test_switch_available_with_missing_key_defaults_to_false(hass) -> None:
    """Proto3 default-omission: a SETTINGS frame omits bool fields that are
    currently False. As soon as any SETTINGS frame arrives, every switch must
    be available — missing keys mean ``False``, not ``unknown``.
    """
    c = make_coordinator(hass, live_settings={"rain_sensor_enabled": True})
    s = switch(c, "anti_theft")
    assert s.available is True
    assert s.is_on is False


@pytest.mark.asyncio
async def test_switch_rain_enable_includes_delay_and_zch(hass) -> None:
    """Activating rain sensor: delay + zch + cutting_height_mm are bundled.

    The cutting submsg (field 4) is atomic: omitting 4.2 resets cutting height
    to 20 mm. Verified against 2026-05-12 app capture.
    """
    c = make_coordinator(
        hass,
        live_settings={
            "rain_sensor_enabled": False,
            "rain_sensor_delay_h": 8,
            "zone_cutting_height_enabled": True,
            "cutting_height_mm": 40,
        },
    )
    s = switch(c, "rain_sensor_enabled")
    await s.async_turn_on()
    c.mqtt.cmd_settings_update.assert_awaited_once_with(
        "MAC1",
        {
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 8,
            "zone_cutting_height_enabled": True,
            "cutting_height_mm": 40,
        },
    )


@pytest.mark.asyncio
async def test_switch_rain_disable_bundles_all_rain_and_cutting(hass) -> None:
    """Deactivating rain sensor bundles delay + zch + cutting_height_mm.

    cmd_settings_update is more strictly atomic than it appears: any write
    omitting the rain/cutting submessages resets them server-side to default.
    build_settings_payload therefore bundles both submessages unconditionally,
    regardless of whether enabled is transitioning to True or False.
    """
    c = make_coordinator(
        hass,
        live_settings={
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 12,
            "zone_cutting_height_enabled": True,
            "cutting_height_mm": 50,
        },
    )
    s = switch(c, "rain_sensor_enabled")
    await s.async_turn_off()
    c.mqtt.cmd_settings_update.assert_awaited_once_with(
        "MAC1",
        {
            "rain_sensor_enabled": False,
            "rain_sensor_delay_h": 12,
            "zone_cutting_height_enabled": True,
            "cutting_height_mm": 50,
        },
    )


@pytest.mark.asyncio
async def test_switch_rain_enable_without_delay_in_live_settings(hass) -> None:
    """Enabling rain sensor when live_settings has no delay: omit delay gracefully."""
    c = make_coordinator(hass, live_settings={"rain_sensor_enabled": False})
    s = switch(c, "rain_sensor_enabled")
    await s.async_turn_on()
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"rain_sensor_enabled": True})


@pytest.mark.asyncio
async def test_switch_raises_when_mqtt_disconnected(hass) -> None:
    c = make_coordinator(hass, live_settings={"rain_sensor_enabled": True}, mqtt_connected=False)
    s = switch(c, "rain_sensor_enabled")
    with pytest.raises(HomeAssistantError) as err:
        await s.async_turn_off()
    assert err.value.translation_key == "mqtt_not_connected"


@pytest.mark.asyncio
async def test_sleep_mode_switch_turn_on_bundles_uniform_and_unknown_11(hass) -> None:
    """Putting the robot to sleep via the HA switch must include fields 9 and
    11 from live_settings — they are bundled by the STIGA.GO app on every
    SETTINGS_UPDATE (capture 2026-06-02) and proto3-atomicity means omitting
    them resets the firmware-side value to default.
    """
    c = make_coordinator(
        hass,
        live_settings={
            "sleep_mode": False,
            "rain_sensor_enabled": False,
            "zone_cutting_height_enabled": True,
            "cutting_height_mm": 30,
            "zone_cutting_height_uniform": True,
            "unknown_11": 105,
        },
    )
    s = switch(c, "sleep_mode")
    assert s.is_on is False
    await s.async_turn_on()
    c.mqtt.cmd_settings_update.assert_awaited_once_with(
        "MAC1",
        {
            "sleep_mode": True,
            "rain_sensor_enabled": False,
            "zone_cutting_height_enabled": True,
            "cutting_height_mm": 30,
            "zone_cutting_height_uniform": True,
            "unknown_11": 105,
        },
    )


@pytest.mark.asyncio
async def test_sleep_mode_switch_optimistic_update_after_turn_off(hass) -> None:
    """After turn_off (wake) the switch must show False even though the
    firmware's SETTINGS response omits sleep_mode (proto3 default = False).
    """
    c = make_coordinator(hass, live_settings={"sleep_mode": True})
    s = switch(c, "sleep_mode")
    assert s.is_on is True
    await s.async_turn_off()
    assert s.is_on is False
