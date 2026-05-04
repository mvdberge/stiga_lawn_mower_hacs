"""Coordinator-side merge logic and MQTT push integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.stiga_mower.coordinator import (
    StigaDataUpdateCoordinator,
    _enrich_status_from_device,
    _merge_live_into_status,
    _merge_sticky_live,
)

# ---------------------------------------------------------------- _merge_live_into_status


def test_merge_returns_base_unchanged_when_live_empty() -> None:
    base = {"current_action": "MOWING", "battery_level": 80}
    assert _merge_live_into_status(base, {}) == base
    # And it doesn't mutate the input
    assert base == {"current_action": "MOWING", "battery_level": 80}


def test_merge_translates_status_type_to_current_action() -> None:
    out = _merge_live_into_status({}, {"status_type": "GOING_HOME"})
    assert out["current_action"] == "GOING_HOME"
    # and `has_data` flips on as soon as any live frame arrives
    assert out["has_data"] is True


def test_merge_mqtt_overrides_rest_for_overlapping_fields() -> None:
    base = {
        "current_action": "WAITING",
        "battery_level": 50,
        "is_docked": False,
        "error_code": None,
    }
    live = {
        "status_type": "MOWING",
        "battery_level": 73,
        "docking": True,
        "info_code": 425,
    }
    out = _merge_live_into_status(base, live)
    assert out["current_action"] == "MOWING"
    assert out["battery_level"] == 73
    assert out["is_docked"] is True
    assert out["error_code"] == 425


def test_merge_passes_through_mqtt_only_fields() -> None:
    live = {
        "status_type": "CUTTING_BORDER",
        "current_zone": 3,
        "zone_completed_pct": 42,
        "garden_completed_pct": 78,
        "signal_quality_pct": 70,
        "satellites": 14,
        "info_label": "RAIN_SENSOR",
        "info_sensor": "rain_sensor",
    }
    out = _merge_live_into_status({}, live)
    for key, value in live.items():
        if key == "status_type":
            continue  # translated above
        assert out[key] == value


def test_merge_live_battery_level_uses_mqtt_value_when_rest_missing() -> None:
    out = _merge_live_into_status({}, {"battery_level": 91})
    assert out["battery_level"] == 91


def test_merge_live_status_type_overrides_stale_rest_charging_flag() -> None:
    # Captured 2026-04-30: REST returned charging:true while MQTT showed
    # status_type=MOWING (stale cloud cache). The live frame must win.
    base = {"battery_charging": True, "current_action": "WORKING"}
    out = _merge_live_into_status(base, {"status_type": "MOWING"})
    assert out["battery_charging"] is False
    assert out["current_action"] == "MOWING"


def test_merge_live_status_charging_sets_battery_charging() -> None:
    out = _merge_live_into_status({"battery_charging": False}, {"status_type": "CHARGING"})
    assert out["battery_charging"] is True


def test_merge_keeps_rest_charging_when_mqtt_has_no_status_type() -> None:
    # A partial MQTT frame (e.g. position-only) must not flip the flag.
    out = _merge_live_into_status({"battery_charging": True}, {"current_zone": 2})
    assert out["battery_charging"] is True


def test_enrich_status_surfaces_dock_firmware_separately() -> None:
    # Robot firmware lives at attributes.firmware_version; the docking
    # station has its own version under settings[0].docking_version, and
    # the two must not be conflated.
    status: dict = {}
    device = {
        "attributes": {
            "firmware_version": "0.2.15.0.0.2.15.0.0.0.1.11",
            "settings": [
                {
                    "parsedSettings": {"cutting_height": "50mm"},
                    "docking_version": "0.0.3.57",
                }
            ],
        }
    }
    _enrich_status_from_device(status, device)
    assert status["dock_firmware"] == "0.0.3.57"
    assert status["cutting_height_mm"] == 50


def test_enrich_status_omits_dock_firmware_when_missing() -> None:
    status: dict = {}
    device = {"attributes": {"settings": [{"parsedSettings": {}}]}}
    _enrich_status_from_device(status, device)
    assert "dock_firmware" not in status


def test_merge_passthrough_total_work_time() -> None:
    # REST `/api/garage` reports total_work_time=0 even when MQTT has the
    # real value (captured 2026-04-30: REST=0, MQTT field 17.9 = 4461 min).
    out = _merge_live_into_status({"total_work_time": 0}, {"total_work_time": 4461})
    assert out["total_work_time"] == 4461


# ---------------------------------------------------------------- _merge_sticky_live


def test_sticky_live_carries_telemetry_across_partial_frames() -> None:
    # Captured 2026-04-30: full frame has battery + network sub-messages,
    # the next mowing-only partial frame omits them. They must not
    # disappear from the cached live state.
    full = {
        "status_type": "MOWING",
        "battery_level": 83,
        "battery_temp_c": 28.4,
        "total_work_time": 4461,
        "rsrp": -94,
        "satellites": 32,
    }
    partial = {
        "status_type": "MOWING",
        "current_zone": 2,
        "zone_completed_pct": 21,
        "garden_completed_pct": 38,
    }
    merged = _merge_sticky_live(full, partial)
    # New frame's keys win
    assert merged["current_zone"] == 2
    assert merged["zone_completed_pct"] == 21
    # Sticky telemetry persists
    assert merged["battery_level"] == 83
    assert merged["battery_temp_c"] == 28.4
    assert merged["total_work_time"] == 4461
    assert merged["rsrp"] == -94
    assert merged["satellites"] == 32


def test_sticky_live_drops_non_sticky_when_absent() -> None:
    # info_code is non-sticky: when the robot exits an error state the next
    # frame omits field 10, and the cached live state must reflect that.
    prev = {"status_type": "BLOCKED", "info_code": 401, "info_label": "BLOCKED"}
    new = {"status_type": "GOING_HOME"}
    merged = _merge_sticky_live(prev, new)
    assert merged["status_type"] == "GOING_HOME"
    assert "info_code" not in merged
    assert "info_label" not in merged


def test_sticky_live_new_frame_wins_for_sticky_fields_too() -> None:
    prev = {"battery_level": 50, "rsrp": -100}
    new = {"battery_level": 65, "rsrp": -90}
    merged = _merge_sticky_live(prev, new)
    assert merged["battery_level"] == 65
    assert merged["rsrp"] == -90


# ---------------------------------------------------------------- Push integration


@pytest.fixture
async def coordinator(hass) -> StigaDataUpdateCoordinator:
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)
    # Pretend a regular REST refresh has populated data so push handlers
    # are allowed to call async_set_updated_data.
    c._devices = [
        {"attributes": {"uuid": "u1", "name": "Bumblebee", "mac_address": "MAC1"}},
    ]
    c.async_set_updated_data(c._build_data(rest_statuses={"u1": {}}))
    return c


def test_attach_mqtt_registers_all_handlers(coordinator: StigaDataUpdateCoordinator) -> None:
    mqtt = MagicMock()
    coordinator.attach_mqtt(mqtt)
    mqtt.set_handlers.assert_called_once()
    kwargs = mqtt.set_handlers.call_args.kwargs
    expected = {
        "on_status",
        "on_position",
        "on_settings",
        "on_schedule",
        "on_base_status",
        "on_connection_change",
    }
    assert set(kwargs) == expected
    # Every handler points back at the coordinator
    for v in kwargs.values():
        assert callable(v)


def test_status_push_merges_into_statuses(coordinator: StigaDataUpdateCoordinator) -> None:
    coordinator._on_mqtt_status("MAC1", {"status_type": "MOWING", "battery_level": 65})
    merged = coordinator.data["statuses"]["u1"]
    assert merged["current_action"] == "MOWING"
    assert merged["battery_level"] == 65
    assert merged["has_data"] is True


def test_status_push_partial_frame_keeps_total_work_time(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    # Replays the captured pattern: full frame followed by a mowing-only
    # partial frame. total_work_time must not flicker back to REST's 0.
    coordinator._on_mqtt_status(
        "MAC1",
        {
            "status_type": "MOWING",
            "battery_level": 83,
            "total_work_time": 4461,
            "rsrp": -94,
        },
    )
    coordinator._on_mqtt_status(
        "MAC1",
        {
            "status_type": "MOWING",
            "current_zone": 2,
            "zone_completed_pct": 21,
        },
    )
    merged = coordinator.data["statuses"]["u1"]
    assert merged["total_work_time"] == 4461
    assert merged["battery_level"] == 83
    assert merged["rsrp"] == -94
    assert merged["current_zone"] == 2


def test_position_push_lands_in_live_position(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    coordinator._on_mqtt_position("MAC1", {"lat_offset_m": 1.0, "lon_offset_m": 2.0})
    assert coordinator.data["live_position"]["MAC1"] == {
        "lat_offset_m": 1.0,
        "lon_offset_m": 2.0,
    }


def test_settings_push_lands_in_live_settings(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    coordinator._on_mqtt_settings("MAC1", {"anti_theft": True})
    assert coordinator.data["live_settings"]["MAC1"] == {"anti_theft": True}


def test_schedule_push_lands_in_live_schedule(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    coordinator._on_mqtt_schedule("MAC1", {"enabled": True, "block_count": 7})
    assert coordinator.data["live_schedule"]["MAC1"]["enabled"] is True


def test_base_status_push_lands_in_live_base_status(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    coordinator._on_mqtt_base_status("BASEMAC", {"status_type": "STANDBY"})
    assert coordinator.data["live_base_status"]["BASEMAC"]["status_type"] == "STANDBY"


def test_connection_change_propagates_to_data(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    coordinator._on_mqtt_connected(True)
    assert coordinator.data["mqtt_connected"] is True
    coordinator._on_mqtt_connected(False)
    assert coordinator.data["mqtt_connected"] is False


def test_publish_update_no_op_before_first_refresh(hass) -> None:
    """Push handlers are silent until the first REST poll completes."""
    api = MagicMock()
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)
    c._devices = [{"attributes": {"uuid": "u1", "mac_address": "MAC1"}}]

    # data is still None — push must not raise nor call async_set_updated_data.
    c._on_mqtt_status("MAC1", {"status_type": "MOWING"})
    assert c.data is None
    # State is buffered though, so the next regular refresh sees it.
    assert c._live_status["MAC1"]["status_type"] == "MOWING"


def test_push_for_unknown_mac_is_buffered_but_not_merged(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    """Frames for an unregistered MAC don't crash and aren't merged anywhere.

    The dispatcher in `mqtt_client.py` already drops unknown-MAC topics, so
    in practice this path is unreachable; we still keep the coordinator
    defensive in case a device gets renamed mid-session.
    """
    coordinator._on_mqtt_status("STRANGE_MAC", {"status_type": "MOWING"})
    # The buffered live_status holds the frame …
    assert "STRANGE_MAC" in coordinator._live_status
    # … but nothing leaks into the registered device's merged status.
    assert "current_action" not in coordinator.data["statuses"]["u1"]
