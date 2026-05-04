"""Tests for Phase 4 sensor additions (MQTT-live sensors)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.stiga_mower.coordinator import StigaDataUpdateCoordinator
from custom_components.stiga_mower.sensor import SENSOR_DESCRIPTIONS, StigaSensor


def _make_coordinator(hass, *, statuses=None):
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)
    c._devices = [{"attributes": {"uuid": "u1", "name": "Bot", "mac_address": "MAC1"}}]
    c.async_set_updated_data(c._build_data(rest_statuses={"u1": statuses or {}}))
    return c


def _sensor(coordinator, key):
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
    device = coordinator.data["devices"][0]
    return StigaSensor(coordinator, device, desc)


# ------------------------------------------------------------------ Zone / progress sensors


@pytest.mark.parametrize(
    "key,status_key,value",
    [
        ("current_zone", "current_zone", 3),
        ("zone_completed_pct", "zone_completed_pct", 42),
        ("garden_completed_pct", "garden_completed_pct", 78),
    ],
)
def test_mqtt_sensor_reads_value(hass, key, status_key, value) -> None:
    c = _make_coordinator(hass, statuses={status_key: value, "has_data": True})
    s = _sensor(c, key)
    assert s.native_value == value
    assert s.available is True


def test_current_zone_unavailable_when_no_data(hass) -> None:
    c = _make_coordinator(hass, statuses={"has_data": False})
    s = _sensor(c, "current_zone")
    assert s.available is False


def test_current_zone_none_when_not_in_status(hass) -> None:
    c = _make_coordinator(hass, statuses={"has_data": True})
    s = _sensor(c, "current_zone")
    assert s.native_value is None


# ------------------------------------------------------------------ GPS / RTK diagnostics


@pytest.mark.parametrize(
    "key,status_key,value",
    [
        ("satellites", "satellites", 12),
        ("rtk_quality_pct", "rtk_quality_pct", 95),
        ("gps_quality", "gps_quality", "GOOD"),
    ],
)
def test_gps_sensor_value(hass, key, status_key, value) -> None:
    c = _make_coordinator(hass, statuses={status_key: value, "has_data": True})
    s = _sensor(c, key)
    assert s.native_value == value


# ------------------------------------------------------------------ Signal quality sensors


@pytest.mark.parametrize(
    "key,status_key,value",
    [
        ("rsrp", "rsrp", -80),
        ("rsrq", "rsrq", -10),
        ("signal_quality_pct", "signal_quality_pct", 70),
    ],
)
def test_signal_sensor_value(hass, key, status_key, value) -> None:
    c = _make_coordinator(hass, statuses={status_key: value, "has_data": True})
    s = _sensor(c, key)
    assert s.native_value == value


# ------------------------------------------------------------------ entity_registry_enabled_default


@pytest.mark.parametrize(
    "key",
    [
        "satellites",
        "rtk_quality_pct",
        "gps_quality",
        "rsrp",
        "rsrq",
        "signal_quality_pct",
    ],
)
def test_diagnostic_sensors_disabled_by_default(key) -> None:
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
    assert desc.entity_registry_enabled_default is False


@pytest.mark.parametrize(
    "key",
    [
        "current_zone",
        "zone_completed_pct",
        "garden_completed_pct",
    ],
)
def test_progress_sensors_enabled_by_default(key) -> None:
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
    # entity_registry_enabled_default defaults to True when not set
    assert desc.entity_registry_enabled_default is not False
