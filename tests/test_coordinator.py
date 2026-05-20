"""Coordinator-side merge logic and MQTT push integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.stiga_mower.api import StigaApiError
from custom_components.stiga_mower.coordinator import (
    _STALE_DATA_THRESHOLD,
    MAX_CONSECUTIVE_FAILURES,
    StigaDataUpdateCoordinator,
    _enrich_status_from_device,
    _extract_perimeter,
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


def test_merge_live_status_clears_stale_rest_error_code() -> None:
    # Captured behaviour: STIGA cloud REST `errorCode` continues to report a
    # past fault long after it cleared. MQTT STATUS frames omit field 10
    # (proto3 default) once the error is gone — that silence must clear the
    # stale REST value rather than leave the `error_active` sensor stuck on
    # "Problem".
    base = {"error_code": 425, "current_action": "WAITING"}
    out = _merge_live_into_status(base, {"status_type": "MOWING"})
    assert out["error_code"] is None


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


def test_merge_passthrough_battery_remaining() -> None:
    # MQTT field 17.9 (decoded as `battery_remaining` mAh from 2.2.4 onwards)
    # must override any stale REST-derived value of the same key.
    out = _merge_live_into_status({"battery_remaining": 4485}, {"battery_remaining": 4645})
    assert out["battery_remaining"] == 4645


# ---------------------------------------------------------------- _merge_sticky_live


def test_sticky_live_carries_telemetry_across_partial_frames() -> None:
    # Captured 2026-04-30: full frame has battery + network sub-messages,
    # the next mowing-only partial frame omits them. They must not
    # disappear from the cached live state.
    full = {
        "status_type": "MOWING",
        "battery_level": 83,
        "battery_temp_c": 28.4,
        "battery_remaining": 4150,
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
    assert merged["battery_remaining"] == 4150
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
        "on_settings",
        "on_schedule",
        "on_base_status",
        "on_base_version",
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


def test_status_push_partial_frame_keeps_battery_remaining(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    # Replays the captured pattern: full frame followed by a mowing-only
    # partial frame. Sticky battery telemetry must not flicker.
    coordinator._on_mqtt_status(
        "MAC1",
        {
            "status_type": "MOWING",
            "battery_level": 83,
            "battery_remaining": 4150,
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
    assert merged["battery_remaining"] == 4150
    assert merged["battery_level"] == 83
    assert merged["rsrp"] == -94
    assert merged["current_zone"] == 2


def test_settings_push_lands_in_live_settings(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    coordinator._on_mqtt_settings("MAC1", {"anti_theft": True})
    assert coordinator.data["live_settings"]["MAC1"] == {"anti_theft": True}


def test_settings_push_merges_partial_frames(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    """A partial SETTINGS frame after a write must not wipe other settings.

    STIGA's firmware replies to cmd_settings_update with a frame containing
    only the touched field. Without merging, the next push would erase every
    other previously-known setting and flick all dependent switches to
    "unavailable".
    """
    coordinator._on_mqtt_settings(
        "MAC1",
        {"rain_sensor_enabled": True, "anti_theft": False, "cutting_height_mm": 30},
    )
    coordinator._on_mqtt_settings("MAC1", {"anti_theft": True})
    assert coordinator.data["live_settings"]["MAC1"] == {
        "rain_sensor_enabled": True,
        "anti_theft": True,
        "cutting_height_mm": 30,
    }


def test_settings_rain_disable_via_app_clears_live_settings(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    """STIGA.GO app disabling rain sensor must clear rain_sensor_enabled in HA.

    Capture 2026-05-12T10:41: app sends SETTINGS_UPDATE {1:{1:0}, 4:{1:1}},
    firmware responds with SETTINGS frame containing only zone_cutting_height_enabled
    (rain submsg absent = disabled at proto3 default). decode_settings emits
    rain_sensor_enabled=False for any non-empty frame without a rain sub-message
    so the coordinator merge correctly overwrites the previously-True value.
    rain_sensor_delay_h is intentionally not touched (delay key absent in output
    when rain submsg absent), leaving any previously-configured delay intact.
    """
    from custom_components.stiga_mower.mqtt_messages import decode_settings
    from custom_components.stiga_mower.protobuf_codec import encode

    coordinator._on_mqtt_settings(
        "MAC1",
        {
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 4,
            "zone_cutting_height_enabled": True,
        },
    )
    assert coordinator.data["live_settings"]["MAC1"]["rain_sensor_enabled"] is True

    # SETTINGS frame after app disables rain: only cutting submsg present
    frame = encode({4: {1: 1}})
    coordinator._on_mqtt_settings("MAC1", decode_settings(frame))

    assert coordinator.data["live_settings"]["MAC1"]["rain_sensor_enabled"] is False


def test_build_settings_payload_bundles_rain_and_cutting(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    """Writes to an unrelated setting must carry rain/cutting state along.

    cmd_settings_update is globally atomic on the firmware: without rain/cutting
    in the outbound payload these submessages get reset to their proto3 default
    server-side — even when the write targets e.g. push_notifications (field 14)
    or obstacle_notifications (field 15). build_settings_payload bundles the
    current rain/cutting values from live_settings so this cannot happen.
    """
    coordinator._on_mqtt_settings(
        "MAC1",
        {
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 8,
            "zone_cutting_height_enabled": True,
            "cutting_height_mm": 40,
        },
    )

    payload = coordinator.build_settings_payload("MAC1", {"push_notifications": True})
    assert payload == {
        "push_notifications": True,
        "rain_sensor_enabled": True,
        "rain_sensor_delay_h": 8,
        "zone_cutting_height_enabled": True,
        "cutting_height_mm": 40,
    }


def test_build_settings_payload_caller_keys_take_precedence(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    """Explicit caller values must not be overridden by live_settings."""
    coordinator._on_mqtt_settings("MAC1", {"rain_sensor_enabled": True})

    payload = coordinator.build_settings_payload("MAC1", {"rain_sensor_enabled": False})
    assert payload["rain_sensor_enabled"] is False


def test_build_settings_payload_skips_missing_live_keys(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    """Keys absent from live_settings stay absent from the payload."""
    payload = coordinator.build_settings_payload("MAC1", {"long_exit": True})
    assert payload == {"long_exit": True}


def test_schedule_push_lands_in_live_schedule(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    coordinator._on_mqtt_schedule("MAC1", {"enabled": True, "block_count": 7})
    assert coordinator.data["live_schedule"]["MAC1"]["enabled"] is True


def test_schedule_push_merges_partial_frames(
    coordinator: StigaDataUpdateCoordinator,
) -> None:
    """cmd_schedule_set_enabled replies with field 1 only — the stored ``days``
    blob must survive the partial frame so the calendar entity stays populated.
    """
    coordinator._on_mqtt_schedule("MAC1", {"enabled": True, "days": [{"slots": set()}] * 7})
    coordinator._on_mqtt_schedule("MAC1", {"enabled": False})
    sched = coordinator.data["live_schedule"]["MAC1"]
    assert sched["enabled"] is False
    assert len(sched["days"]) == 7


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


# ---------------------------------------------------------------- _extract_perimeter


def test_extract_perimeter_zone_elements() -> None:
    perimeter = {
        "data": {
            "attributes": {
                "preview": {
                    "m2Area": 661.48,
                    "zones": {
                        "num": 3,
                        "m2Area": 661.48,
                        "elements": [
                            {"id": 1, "m2Area": 108.94, "numPoints": 38},
                            {"id": 2, "m2Area": 128.84, "numPoints": 49},
                            {"id": 3, "m2Area": 5.46, "numPoints": 11},
                        ],
                    },
                    "obstacles": {"num": 2, "m2Area": 12.5},
                }
            }
        }
    }
    out = _extract_perimeter(perimeter)
    assert out["zone_count"] == 3
    assert out["garden_area_m2"] == 661.48
    assert out["obstacle_count"] == 2
    assert len(out["zone_elements"]) == 3
    assert out["zone_elements"][0] == {"id": 1, "area_m2": 108.94, "num_points": 38}
    assert out["zone_elements"][1] == {"id": 2, "area_m2": 128.84, "num_points": 49}
    assert out["zone_elements"][2] == {"id": 3, "area_m2": 5.46, "num_points": 11}


def test_extract_perimeter_no_zone_elements_when_empty() -> None:
    perimeter = {
        "data": {
            "attributes": {
                "preview": {
                    "zones": {"num": 0, "elements": []},
                }
            }
        }
    }
    out = _extract_perimeter(perimeter)
    assert "zone_elements" not in out


def test_extract_perimeter_zone_elements_area_rounded() -> None:
    perimeter = {
        "data": {
            "attributes": {
                "preview": {
                    "zones": {
                        "num": 1,
                        "elements": [{"id": 1, "m2Area": 108.9412345, "numPoints": 5}],
                    }
                }
            }
        }
    }
    out = _extract_perimeter(perimeter)
    assert out["zone_elements"][0]["area_m2"] == 108.94


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


# ---------------------------------------------------------------- REST failure handling


@pytest.fixture
def rest_coordinator(hass) -> StigaDataUpdateCoordinator:
    """Coordinator wired with an AsyncMock API for driving _async_update_data."""
    api = MagicMock()
    api.get_devices = AsyncMock(
        return_value=[
            {"attributes": {"uuid": "u1", "name": "Bumblebee", "mac_address": "MAC1"}},
        ]
    )
    api.get_device_status = AsyncMock(return_value={"has_data": True, "battery_level": 50})
    api.get_device_extended = AsyncMock(return_value={})
    api.get_perimeter = AsyncMock(return_value={})
    api.get_bases = AsyncMock(return_value=[])
    entry = MagicMock(data={"email": "e", "password": "p"})
    return StigaDataUpdateCoordinator(hass, entry, api)


async def test_poll_success_bumps_last_rest_success(
    rest_coordinator: StigaDataUpdateCoordinator,
) -> None:
    """Successful poll updates the freshness timestamp."""
    assert rest_coordinator._last_rest_success is None
    await rest_coordinator.async_refresh()
    assert rest_coordinator._last_rest_success is not None
    assert rest_coordinator.rest_data_fresh is True
    assert rest_coordinator._consecutive_failures == 0


async def test_poll_with_all_status_failures_does_not_bump_timestamp(
    rest_coordinator: StigaDataUpdateCoordinator,
) -> None:
    """When every per-device status call fails, the poll must count as failed.

    Regression: previously `_last_rest_success` was bumped unconditionally
    after the (silently-caught) device-list fetch, so `rest_data_fresh`
    stayed True forever and the 10-minute grace never engaged.
    """
    # First a real success to populate data so subsequent failures don't raise.
    await rest_coordinator.async_refresh()
    baseline_ts = rest_coordinator._last_rest_success

    rest_coordinator.api.get_device_status = AsyncMock(side_effect=StigaApiError("503"))
    await rest_coordinator.async_refresh()

    assert rest_coordinator._last_rest_success == baseline_ts  # not bumped
    assert rest_coordinator._consecutive_failures == 1


async def test_consecutive_failures_eventually_marks_data_stale(
    rest_coordinator: StigaDataUpdateCoordinator,
) -> None:
    """After `_STALE_DATA_THRESHOLD` of failures, rest_data_fresh flips to False."""
    await rest_coordinator.async_refresh()
    rest_coordinator.api.get_device_status = AsyncMock(side_effect=StigaApiError("down"))

    # Backdate the last-success timestamp past the grace window.
    rest_coordinator._last_rest_success -= _STALE_DATA_THRESHOLD * 2
    await rest_coordinator.async_refresh()

    assert rest_coordinator.rest_data_fresh is False
    assert rest_coordinator._consecutive_failures == 1


async def test_consecutive_failures_raises_issue_after_threshold(
    rest_coordinator: StigaDataUpdateCoordinator,
) -> None:
    """The issue-registry entry is created once MAX_CONSECUTIVE_FAILURES is hit."""
    await rest_coordinator.async_refresh()
    rest_coordinator.api.get_device_status = AsyncMock(side_effect=StigaApiError("down"))
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        await rest_coordinator.async_refresh()
    assert rest_coordinator._consecutive_failures == MAX_CONSECUTIVE_FAILURES


async def test_partial_status_failure_still_counts_as_success(
    hass,
) -> None:
    """If at least one device's status succeeds, the poll is fresh enough."""
    api = MagicMock()
    api.get_devices = AsyncMock(
        return_value=[
            {"attributes": {"uuid": "u1", "mac_address": "MAC1"}},
            {"attributes": {"uuid": "u2", "mac_address": "MAC2"}},
        ]
    )
    api.get_device_status = AsyncMock(
        side_effect=[StigaApiError("u1 down"), {"has_data": True, "battery_level": 42}]
    )
    api.get_device_extended = AsyncMock(return_value={})
    api.get_perimeter = AsyncMock(return_value={})
    api.get_bases = AsyncMock(return_value=[])
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)

    await c.async_refresh()

    assert c._last_rest_success is not None
    assert c._consecutive_failures == 0
    assert c.rest_data_fresh is True
