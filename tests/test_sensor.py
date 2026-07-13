"""Tests for the StigaSensor entity (MQTT-live sensors)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.stiga_mower.coordinator import StigaDataUpdateCoordinator
from custom_components.stiga_mower.sensor import SENSOR_DESCRIPTIONS, StigaSensor


def _make_coordinator(hass, *, statuses=None, mqtt_connected=True, meta=None):
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)
    c._devices = [{"attributes": {"uuid": "u1", "name": "Bot", "mac_address": "MAC1"}}]
    c._mqtt_connected = mqtt_connected
    if meta is not None:
        c._meta = {"u1": meta}
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


@pytest.mark.parametrize(
    "key,status_key,value",
    [
        ("current_zone", "current_zone", 3),
        ("zone_completed_pct", "zone_completed_pct", 42),
        ("garden_completed_pct", "garden_completed_pct", 78),
    ],
)
def test_mqtt_sensor_unavailable_when_mqtt_disconnected(hass, key, status_key, value) -> None:
    # MQTT-only fields must go unavailable when MQTT drops, even though the last
    # received value is still cached in _live_status (which is never cleared).
    c = _make_coordinator(
        hass, statuses={status_key: value, "has_data": True}, mqtt_connected=False
    )
    s = _sensor(c, key)
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
        "rsrp",
        "rsrq",
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


# ------------------------------------------------------------------ state_class


def test_current_zone_has_no_state_class() -> None:
    # current_zone is a categorical zone identifier, not a continuous quantity,
    # so it must not carry a state_class (which would trigger meaningless
    # long-term statistics over zone IDs).
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "current_zone")
    assert desc.state_class is None


# ------------------------------------------------------------------ meta-sourced sensors


def test_meta_sensor_unavailable_when_field_missing(hass) -> None:
    # A meta entry exists for the device, but this sensor's own status_key is
    # absent — it must report unavailable rather than expose a None value.
    c = _make_coordinator(hass, meta={"model_name": "A 15v"})
    s = _sensor(c, "garden_area")
    assert s.available is False
    assert s.native_value is None


def test_meta_sensor_available_when_field_present(hass) -> None:
    c = _make_coordinator(hass, meta={"garden_area_m2": 656})
    s = _sensor(c, "garden_area")
    assert s.available is True
    assert s.native_value == 656
