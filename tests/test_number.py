"""Tests for the StigaNumber entity (cutting height)."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError

from ._entity_helpers import make_coordinator, number


def test_number_reads_from_live_settings(hass) -> None:
    c = make_coordinator(hass, live_settings={"cutting_height_mm": 40})
    n = number(c)
    assert n.native_value == 40.0
    assert n.available is True


def test_number_reads_from_rest_status_fallback(hass) -> None:
    c = make_coordinator(hass, rest_status={"cutting_height_mm": 35, "has_data": True})
    n = number(c)
    assert n.native_value == 35.0


def test_number_unavailable_when_no_value(hass) -> None:
    c = make_coordinator(hass)
    n = number(c)
    assert n.available is False


@pytest.mark.asyncio
async def test_number_set_value_calls_mqtt(hass) -> None:
    c = make_coordinator(hass, live_settings={"cutting_height_mm": 40})
    n = number(c)
    await n.async_set_native_value(45)
    c.mqtt.cmd_settings_update.assert_awaited_once_with("MAC1", {"cutting_height_mm": 45})


@pytest.mark.asyncio
async def test_number_cutting_height_bundles_zone_cutting_height_enabled(hass) -> None:
    """Cutting submsg (field 4) is atomic: omitting 4.1 resets zone_cutting_height_enabled.

    Verified against capture: SETTINGS frames always carry both 4.1 and 4.2.
    """
    c = make_coordinator(
        hass,
        live_settings={"cutting_height_mm": 40, "zone_cutting_height_enabled": True},
    )
    n = number(c)
    await n.async_set_native_value(35)
    c.mqtt.cmd_settings_update.assert_awaited_once_with(
        "MAC1",
        {"cutting_height_mm": 35, "zone_cutting_height_enabled": True},
    )


@pytest.mark.asyncio
async def test_number_cutting_height_optimistic_update(hass) -> None:
    """Cutting height at its proto3 default (20mm = index 0) is omitted from the
    firmware response; apply_live_settings must reflect the new value immediately.
    """
    c = make_coordinator(hass, live_settings={"cutting_height_mm": 40})
    n = number(c)
    assert n.native_value == 40.0
    await n.async_set_native_value(20)
    assert n.native_value == 20.0


@pytest.mark.asyncio
async def test_number_raises_when_mqtt_disconnected(hass) -> None:
    c = make_coordinator(hass, live_settings={"cutting_height_mm": 40}, mqtt_connected=False)
    n = number(c)
    with pytest.raises(HomeAssistantError) as err:
        await n.async_set_native_value(45)
    assert err.value.translation_key == "mqtt_not_connected"
