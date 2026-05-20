"""Tests for the StigaSelect and StigaScheduleModeSelect entities."""

from __future__ import annotations

import pytest

from custom_components.stiga_mower.mqtt_messages import pack_schedule

from ._entity_helpers import make_coordinator, schedule_mode_select, select

# ---------------------------------------------------------------- rain_delay select


def test_select_rain_delay_current_option(hass) -> None:
    # live_settings stores decoded hours directly (decode_settings maps index->hours)
    c = make_coordinator(hass, live_settings={"rain_sensor_delay_h": 8})
    s = select(c, "rain_sensor_delay")
    assert s.current_option == "8"


def test_select_unavailable_when_no_live_settings(hass) -> None:
    c = make_coordinator(hass)
    s = select(c, "rain_sensor_delay")
    assert s.available is False


def test_select_rain_delay_defaults_to_4h_when_key_missing(hass) -> None:
    """Proto3 default-omission: SETTINGS frames omit rain_sensor_delay_h when
    the wire index is 0 (= 4 hours). The select must still be available and
    report ``"4"`` instead of staying permanently ``unavailable``.
    """
    c = make_coordinator(hass, live_settings={"rain_sensor_enabled": True})
    s = select(c, "rain_sensor_delay")
    assert s.available is True
    assert s.current_option == "4"


def test_select_unavailable_when_mqtt_disconnected(hass) -> None:
    c = make_coordinator(hass, live_settings={"rain_sensor_delay_h": 8}, mqtt_connected=False)
    s = select(c, "rain_sensor_delay")
    assert s.available is False


@pytest.mark.asyncio
async def test_select_rain_delay_includes_enabled_and_zch(hass) -> None:
    """Changing rain delay: enabled flag + zch + cutting_height_mm bundled.

    The cutting submsg (field 4) is atomic: omitting 4.2 resets cutting height
    to 20 mm. Verified against 2026-05-12 app capture.
    """
    c = make_coordinator(
        hass,
        live_settings={
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 4,
            "zone_cutting_height_enabled": True,
            "cutting_height_mm": 45,
        },
    )
    s = select(c, "rain_sensor_delay")
    await s.async_select_option("12")
    c.mqtt.cmd_settings_update.assert_awaited_once_with(
        "MAC1",
        {
            "rain_sensor_delay_h": 12,
            "rain_sensor_enabled": True,
            "zone_cutting_height_enabled": True,
            "cutting_height_mm": 45,
        },
    )


@pytest.mark.asyncio
async def test_select_rain_delay_without_enabled_in_live_settings(hass) -> None:
    """Changing delay when live_settings has no enabled flag: omit gracefully."""
    c = make_coordinator(hass, live_settings={"rain_sensor_delay_h": 4})
    s = select(c, "rain_sensor_delay")
    await s.async_select_option("8")
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"rain_sensor_delay_h": 8})


@pytest.mark.asyncio
async def test_select_rain_delay_optimistic_update_to_4h(hass) -> None:
    """Selecting 4 h must show immediately even if the firmware omits the field.

    The wire index for 4 h is 0 (proto3 default), so the firmware's SETTINGS
    response omits rain[2]. The coordinator merge cannot detect the change;
    the optimistic apply_live_settings in async_select_option must apply it.
    """
    c = make_coordinator(
        hass,
        live_settings={
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 8,
            "zone_cutting_height_enabled": True,
        },
    )
    s = select(c, "rain_sensor_delay")
    assert s.current_option == "8"
    await s.async_select_option("4")
    assert s.current_option == "4"


@pytest.mark.asyncio
async def test_select_raises_on_unknown_option(hass) -> None:
    c = make_coordinator(hass, live_settings={"rain_sensor_delay_h": 8})
    s = select(c, "rain_sensor_delay")
    with pytest.raises(Exception, match="Unknown option"):
        await s.async_select_option("spirograph")


# ---------------------------------------------------------------- schedule_mode select


def test_schedule_mode_options_are_manual_and_auto(hass) -> None:
    c = make_coordinator(hass, live_schedule={"enabled": True})
    s = schedule_mode_select(c)
    assert s.options == ["manual", "auto"]


def test_schedule_mode_current_option_auto_when_enabled(hass) -> None:
    c = make_coordinator(hass, live_schedule={"enabled": True})
    s = schedule_mode_select(c)
    assert s.available is True
    assert s.current_option == "auto"


def test_schedule_mode_current_option_manual_when_disabled(hass) -> None:
    c = make_coordinator(hass, live_schedule={"enabled": False})
    s = schedule_mode_select(c)
    assert s.available is True
    assert s.current_option == "manual"


def test_schedule_mode_defaults_to_manual_when_key_missing(hass) -> None:
    """Proto3 default-omission: a disabled schedule (field 1 = False) is
    omitted on the wire. A populated live_schedule entry without the key
    therefore means "manual", not "unavailable".
    """
    c = make_coordinator(hass, live_schedule={"days": []})
    s = schedule_mode_select(c)
    assert s.available is True
    assert s.current_option == "manual"


def test_schedule_mode_unavailable_without_live_schedule(hass) -> None:
    c = make_coordinator(hass)
    s = schedule_mode_select(c)
    assert s.available is False
    assert s.current_option is None


def test_schedule_mode_unavailable_when_mqtt_disconnected(hass) -> None:
    c = make_coordinator(hass, live_schedule={"enabled": True}, mqtt_connected=False)
    s = schedule_mode_select(c)
    assert s.available is False


def test_schedule_mode_has_no_entity_category(hass) -> None:
    """The schedule mode is a user-facing operating control and must live in
    the default "Controls" HA category — not in "Configuration".
    """
    c = make_coordinator(hass, live_schedule={"enabled": True})
    s = schedule_mode_select(c)
    assert s.entity_category is None


@pytest.mark.asyncio
async def test_schedule_mode_select_auto_sends_true(hass) -> None:
    # No days in live_schedule → always bundles an empty blob (never sends blob=None)
    c = make_coordinator(hass, live_schedule={"enabled": False})
    s = schedule_mode_select(c)
    await s.async_select_option("auto")
    c.mqtt.cmd_schedule_set_enabled.assert_awaited_once_with("MAC1", True, blob=pack_schedule([]))


@pytest.mark.asyncio
async def test_schedule_mode_select_manual_sends_false(hass) -> None:
    # No days in live_schedule → always bundles an empty blob (never sends blob=None)
    c = make_coordinator(hass, live_schedule={"enabled": True})
    s = schedule_mode_select(c)
    await s.async_select_option("manual")
    c.mqtt.cmd_schedule_set_enabled.assert_awaited_once_with("MAC1", False, blob=pack_schedule([]))


@pytest.mark.asyncio
async def test_schedule_mode_select_auto_bundles_blob(hass) -> None:
    """Enabling schedule must bundle the current blob.

    SCHEDULING_SETTINGS_UPDATE is atomic on the firmware: sending field 1
    without field 2 resets the schedule blob to empty, wiping all mowing times.
    Verified against 2026-05-12 app capture: both {1:1, 2:<blob>} sent together.
    """
    days = [{"slots": {0, 1, 2}} if i == 0 else {"slots": set()} for i in range(7)]
    expected_blob = pack_schedule(days)
    c = make_coordinator(hass, live_schedule={"enabled": False, "days": days})
    s = schedule_mode_select(c)
    await s.async_select_option("auto")
    c.mqtt.cmd_schedule_set_enabled.assert_awaited_once_with("MAC1", True, blob=expected_blob)


@pytest.mark.asyncio
async def test_schedule_mode_select_manual_bundles_blob(hass) -> None:
    """Disabling schedule must also bundle the blob to preserve mowing times."""
    days = [{"slots": {10, 20}} if i == 3 else {"slots": set()} for i in range(7)]
    expected_blob = pack_schedule(days)
    c = make_coordinator(hass, live_schedule={"enabled": True, "days": days})
    s = schedule_mode_select(c)
    await s.async_select_option("manual")
    c.mqtt.cmd_schedule_set_enabled.assert_awaited_once_with("MAC1", False, blob=expected_blob)


@pytest.mark.asyncio
async def test_schedule_mode_unknown_option_raises(hass) -> None:
    c = make_coordinator(hass, live_schedule={"enabled": True})
    s = schedule_mode_select(c)
    with pytest.raises(Exception, match="Unknown option"):
        await s.async_select_option("disco")


@pytest.mark.asyncio
async def test_schedule_mode_raises_when_mqtt_disconnected(hass) -> None:
    c = make_coordinator(hass, live_schedule={"enabled": True}, mqtt_connected=False)
    s = schedule_mode_select(c)
    with pytest.raises(Exception, match="MQTT not connected"):
        await s.async_select_option("auto")


@pytest.mark.asyncio
async def test_schedule_mode_optimistic_update_on_disable(hass) -> None:
    """Selecting 'manual' must show immediately without waiting for firmware.

    enabled=False is the proto3 default and gets omitted from the firmware's
    SCHEDULING_SETTINGS response; the coordinator merge cannot detect the
    transition — apply_live_schedule must update live_schedule immediately.
    """
    c = make_coordinator(hass, live_schedule={"enabled": True})
    s = schedule_mode_select(c)
    assert s.current_option == "auto"
    await s.async_select_option("manual")
    assert s.current_option == "manual"
