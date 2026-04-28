"""Tests for binary_sensor entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.stiga_mower.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
    StigaBinarySensor,
    _info_sensor_value,
)
from custom_components.stiga_mower.coordinator import StigaDataUpdateCoordinator

# ------------------------------------------------------------------ helpers


def _make_coordinator(hass, *, statuses=None, mqtt_connected=False, live_settings=None):
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)
    c._devices = [{"attributes": {"uuid": "u1", "name": "Bot", "mac_address": "MAC1"}}]
    c._mqtt_connected = mqtt_connected
    c._live_settings = live_settings or {}
    c.async_set_updated_data(c._build_data(rest_statuses={"u1": statuses or {}}))
    return c


def _sensor(coordinator, key):
    desc = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == key)
    device = coordinator.data["devices"][0]
    return StigaBinarySensor(coordinator, device, desc)


# ------------------------------------------------------------------ _info_sensor_value


def test_info_sensor_value_matches() -> None:
    assert _info_sensor_value({"info_sensor": "rain_sensor"}, "rain_sensor") is True


def test_info_sensor_value_no_match() -> None:
    assert _info_sensor_value({"info_sensor": "lift_sensor"}, "rain_sensor") is False


def test_info_sensor_value_missing_key() -> None:
    assert _info_sensor_value({}, "rain_sensor") is False


# ------------------------------------------------------------------ mqtt_connected


def test_mqtt_connected_true(hass) -> None:
    c = _make_coordinator(hass, mqtt_connected=True)
    s = _sensor(c, "mqtt_connected")
    assert s.is_on is True
    assert s.available is True


def test_mqtt_connected_false(hass) -> None:
    c = _make_coordinator(hass, mqtt_connected=False)
    s = _sensor(c, "mqtt_connected")
    assert s.is_on is False
    # still available — connectivity sensor is always known
    assert s.available is True


# ------------------------------------------------------------------ is_docked


def test_is_docked_true(hass) -> None:
    c = _make_coordinator(hass, statuses={"is_docked": True, "has_data": True})
    s = _sensor(c, "is_docked")
    assert s.is_on is True


def test_is_docked_false(hass) -> None:
    c = _make_coordinator(hass, statuses={"is_docked": False, "has_data": True})
    s = _sensor(c, "is_docked")
    assert s.is_on is False


def test_is_docked_unavailable_when_no_data(hass) -> None:
    c = _make_coordinator(hass, statuses={"has_data": False})
    s = _sensor(c, "is_docked")
    assert s.available is False


# ------------------------------------------------------------------ battery_charging


def test_battery_charging_on(hass) -> None:
    c = _make_coordinator(hass, statuses={"battery_charging": True, "has_data": True})
    s = _sensor(c, "battery_charging")
    assert s.is_on is True


def test_battery_charging_off(hass) -> None:
    c = _make_coordinator(hass, statuses={"battery_charging": False, "has_data": True})
    s = _sensor(c, "battery_charging")
    assert s.is_on is False


def test_battery_charging_none_returns_none(hass) -> None:
    c = _make_coordinator(hass, statuses={"has_data": True})
    s = _sensor(c, "battery_charging")
    assert s.is_on is None


# ------------------------------------------------------------------ error_active


def test_error_active_when_nonzero_error_code(hass) -> None:
    c = _make_coordinator(hass, statuses={"error_code": 425, "has_data": True})
    s = _sensor(c, "error_active")
    assert s.is_on is True


def test_error_active_false_when_zero(hass) -> None:
    c = _make_coordinator(hass, statuses={"error_code": 0, "has_data": True})
    s = _sensor(c, "error_active")
    assert s.is_on is False


def test_error_active_false_when_none(hass) -> None:
    c = _make_coordinator(hass, statuses={"has_data": True})
    s = _sensor(c, "error_active")
    assert s.is_on is None


# ------------------------------------------------------------------ info_sensor sensors


@pytest.mark.parametrize(
    "sensor_key,sensor_name",
    [
        ("rain_sensor", "rain_sensor"),
        ("lift_sensor", "lift_sensor"),
        ("bump_sensor", "bump_sensor"),
        ("slope_sensor", "slope_sensor"),
        ("lid_sensor", "lid_sensor"),
    ],
)
def test_info_sensor_active_when_matching(hass, sensor_key, sensor_name) -> None:
    c = _make_coordinator(
        hass,
        statuses={"info_sensor": sensor_name, "has_data": True},
    )
    s = _sensor(c, sensor_key)
    assert s.is_on is True


@pytest.mark.parametrize(
    "sensor_key",
    [
        "rain_sensor",
        "lift_sensor",
        "bump_sensor",
        "slope_sensor",
        "lid_sensor",
    ],
)
def test_info_sensor_off_when_different_code(hass, sensor_key) -> None:
    c = _make_coordinator(
        hass,
        statuses={"info_sensor": "wheel_trouble", "has_data": True},
    )
    s = _sensor(c, sensor_key)
    assert s.is_on is False


@pytest.mark.parametrize(
    "sensor_key",
    [
        "rain_sensor",
        "lift_sensor",
        "bump_sensor",
        "slope_sensor",
        "lid_sensor",
    ],
)
def test_info_sensor_off_when_no_info_sensor(hass, sensor_key) -> None:
    c = _make_coordinator(hass, statuses={"has_data": True})
    s = _sensor(c, sensor_key)
    assert s.is_on is False
