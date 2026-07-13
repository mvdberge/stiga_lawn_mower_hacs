"""Tests for the StigaLawnMower entity (pause / dock action paths)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity

from custom_components.stiga_mower.lawn_mower import (
    MOWING_MODE_LABELS,
    MOWING_MODE_TO_ACTIVITY,
    StigaLawnMower,
)

from ._entity_helpers import device, make_coordinator

# Labels considered consistent with each activity for integer mowingMode codes.
# The activity is the trusted meaning; a code's label must describe that state.
_CONSISTENT_LABELS: dict[LawnMowerActivity, set[str]] = {
    LawnMowerActivity.MOWING: {"Mowing", "Border mowing"},
    LawnMowerActivity.PAUSED: {"Paused"},
    LawnMowerActivity.DOCKED: {"Sleeping/Charging", "Docked"},
    LawnMowerActivity.ERROR: {"Error", "Locked"},
}


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


# ---------------------------------------------------------------- state mapping


def test_integer_mowing_mode_activity_label_consistency() -> None:
    """Every integer mowingMode code's label matches its activity (task 15)."""
    for code, activity in MOWING_MODE_TO_ACTIVITY.items():
        if not isinstance(code, int):
            continue
        label = MOWING_MODE_LABELS[code]
        assert label in _CONSISTENT_LABELS[activity], (
            f"code {code!r}: activity {activity} vs label {label!r}"
        )


def test_codes_2_and_8_reconciled() -> None:
    """Codes 2 and 8 (and 0) yield a consistent activity+label pair (task 15)."""
    assert MOWING_MODE_TO_ACTIVITY[2] is LawnMowerActivity.PAUSED
    assert MOWING_MODE_LABELS[2] == "Paused"
    assert MOWING_MODE_TO_ACTIVITY[8] is LawnMowerActivity.DOCKED
    assert MOWING_MODE_LABELS[8] == "Docked"
    assert MOWING_MODE_TO_ACTIVITY[0] is LawnMowerActivity.DOCKED
    assert MOWING_MODE_LABELS[0] == "Docked"


def test_docked_current_action_maps_to_docked(hass) -> None:
    """currentAction 'DOCKED' resolves to DOCKED with a matching label (task 46)."""
    c = make_coordinator(hass, rest_status={"has_data": True, "current_action": "DOCKED"})
    mower = StigaLawnMower(c, device(c))
    assert mower.activity is LawnMowerActivity.DOCKED
    assert mower.extra_state_attributes["mowing_mode_label"] == "Docked"


def test_unknown_current_action_without_mode_warns(hass, caplog) -> None:
    """An unrecognised currentAction string is surfaced as a warning (task 49)."""
    c = make_coordinator(hass, rest_status={"has_data": True, "current_action": "WARP_SPEED"})
    mower = StigaLawnMower(c, device(c))
    with caplog.at_level(logging.WARNING):
        assert mower.activity is LawnMowerActivity.PAUSED
    assert "WARP_SPEED" in caplog.text
