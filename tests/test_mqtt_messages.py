"""Tests for the MQTT message decoders/encoders.

These exercise the pure decoder functions: bytes-in → dict-out. We build
the protobuf inputs through ``protobuf_codec.encode`` (already covered by
``test_protobuf_codec.py``).
"""

from __future__ import annotations

import json

import pytest

from custom_components.stiga_mower import mqtt_constants as mc
from custom_components.stiga_mower import mqtt_messages as mm
from custom_components.stiga_mower import protobuf_codec as pb


def _varint_bytes(value: int) -> bytes:
    """Plain unsigned varint encoder for test-fixture construction."""
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _wrap_len(field: int, payload: bytes) -> bytes:
    """Wrap a raw byte payload as a LEN-delimited field."""
    return _varint_bytes((field << 3) | 2) + _varint_bytes(len(payload)) + payload


# ---------------------------------------------------------------- decode_status


def test_decode_status_full_frame() -> None:
    """A frame with every documented field roundtrips to descriptive keys.

    Field mapping verified against live captures (2026-04-30):
      17 = battery sub-msg: {1:capacity_mah, 2:level%, 7:temp_c, 9:work_time_min, 12:current_a}
      18 = mowing sub-msg:  {1:zone, 2:zone_pct, 3:garden_pct, 4:{1:level%, 2:voltage_v, 3:charging}}
      19 = location sub-msg:{1:gps_quality_enum, 2:satellites, 5:rtk_quality_pct, 6:rtk_fix_type}
      20 = network sub-msg: {3:{4:kind, 5:type, 6:band, 10:rsrp, 11:signal_quality_pct(-32768=N/A), 12:rsrq}}
    """
    payload = pb.encode(
        {
            1: 1,  # status_valid
            2: 1,  # operable
            3: 32,  # CUTTING_BORDER
            4: {1: 2, 2: 22},  # status_error
            10: {1: 0x01A9, 2: 0, 3: 0, 4: 0},  # info_code: RAIN_SENSOR
            13: 0,  # not docking
            17: {1: 5000, 2: 87},  # battery: capacity + level
            18: {1: 3, 2: 42, 3: 78, 4: {1: 85, 2: 11.5, 3: 1}},  # mowing + batt detail
            # location: gps_quality=GOOD, satellites=14, rtk_quality=95%, rtk_fixed
            19: {1: 0, 2: 14, 5: 95, 6: 4},
            20: {3: {4: 5, 5: 9, 6: 3, 10: -90, 11: 70, 12: -10}},  # network
        }
    )

    out = mm.decode_status(payload)

    assert out["status_valid"] is True
    assert out["operable"] is True
    assert out["status_type"] == "CUTTING_BORDER"
    assert out["error_code1"] == 2
    assert out["error_code2"] == 22
    assert out["info_code"] == 0x01A9
    assert out["info_label"] == "RAIN_SENSOR"
    assert out["info_sensor"] == "rain_sensor"
    assert out["docking"] is False
    assert out["battery_capacity_mah"] == 5000
    assert out["battery_level"] == 87  # from 17.2
    assert out["battery_voltage"] == pytest.approx(11.5)
    assert (
        "battery_charging" not in out
    )  # derived from status_type in coordinator, not STATUS frame
    assert out["current_zone"] == 3
    assert out["zone_completed_pct"] == 42
    assert out["garden_completed_pct"] == 78
    assert out["gps_quality"] == "GOOD"
    assert out["satellites"] == 14
    assert out["rtk_quality_pct"] == 95
    assert out["rtk_fix_type"] == 4
    assert out["signal_quality_pct"] == 70
    assert out["rsrp"] == -90
    assert out["rsrq"] == -10


def test_decode_status_minimal_frame() -> None:
    """Mowers in early-init can omit nested groups; we keep keys absent."""
    payload = pb.encode({1: 1, 3: 4})  # status_valid + DOCKED
    out = mm.decode_status(payload)
    assert out == {"status_valid": True, "status_type": "DOCKED"}


def test_decode_status_mowing_progress_defaults_to_zero() -> None:
    """Proto3 default-omission: when the mowing sub-message is present but its
    scalar fields sit at the wire default (0), the encoder drops them. The
    decoder must still surface ``current_zone`` / ``zone_completed_pct`` /
    ``garden_completed_pct`` as ``0`` so the progress sensors don't flicker to
    ``unavailable`` at the start of a cycle (or while the mower sits in zone
    0). The submsg is signalled by any non-default child — here field 4.
    """
    payload = pb.encode({18: {4: {1: 50}}})  # mowing submsg present, fields 1-3 omitted
    out = mm.decode_status(payload)
    assert out["current_zone"] == 0
    assert out["zone_completed_pct"] == 0
    assert out["garden_completed_pct"] == 0


def test_decode_status_mowing_progress_absent_when_submsg_missing() -> None:
    """When the mowing sub-message is *entirely* absent (mower idle/docked,
    no mowing telemetry), the progress sensors must stay absent, not default
    to 0 — otherwise idle robots would report a fake "zone 0, 0 % completed"
    state.
    """
    payload = pb.encode({1: 1, 3: 4})  # status_valid + DOCKED, no field 18
    out = mm.decode_status(payload)
    assert "current_zone" not in out
    assert "zone_completed_pct" not in out
    assert "garden_completed_pct" not in out


def test_decode_status_signal_quality_sentinel_dropped() -> None:
    """Modem reports -32768 in 20.3.11 when signal quality is unavailable;
    the decoder must omit the key rather than surface the sentinel."""
    payload = pb.encode({20: {3: {10: -90, 11: -32768, 12: -8}}})
    out = mm.decode_status(payload)
    assert out["rsrp"] == -90
    assert out["rsrq"] == -8
    assert "signal_quality_pct" not in out


def test_decode_status_unknown_status_type_passthrough() -> None:
    payload = pb.encode({3: 99})
    out = mm.decode_status(payload)
    # Unknown numeric codes are passed through verbatim so future firmware
    # values surface in diagnostics rather than getting silently dropped.
    assert out == {"status_type": 99}


def test_decode_status_unknown_info_code_keeps_raw_code() -> None:
    payload = pb.encode({10: {1: 0x9999}})
    out = mm.decode_status(payload)
    assert out["info_code"] == 0x9999
    assert out["info_label"] is None
    assert "info_sensor" not in out


def test_decode_status_empty_payload() -> None:
    assert mm.decode_status(b"") == {}


def test_decode_status_malformed_does_not_raise() -> None:
    # truncated VARINT — decoder swallows and returns {}
    assert mm.decode_status(b"\x80") == {}


# ---------------------------------------------------------------- decode_settings


def test_decode_settings_full_frame() -> None:
    payload = pb.encode(
        {
            1: {1: 1, 2: 1},  # rain sensor on, 8h delay
            2: 0,  # keyboard lock off
            4: {1: 1, 2: 5},  # zone height enabled, 45 mm
            6: 1,  # anti-theft on
            7: 0,  # smart cut height off
            8: {1: 1, 3: 2},  # long exit on, mode field 3 (decoded-only, ignored)
            9: 1,  # uniform height (decoded-only, ignored)
            14: {1: 1},  # push notifications on
            15: {1: 0},  # obstacle notifications off
        }
    )
    out = mm.decode_settings(payload)
    assert out == {
        "rain_sensor_enabled": True,
        "rain_sensor_delay_h": 8,
        "keyboard_lock": False,
        "zone_cutting_height_enabled": True,
        "cutting_height_mm": 45,
        "anti_theft": True,
        "smart_cutting_height": False,
        "long_exit": True,
        "push_notifications": True,
        "obstacle_notifications": False,
    }


def test_decode_settings_rain_delay_4h_via_empty_submsg() -> None:
    """User-reported bug: setting the rain delay to 4 h in the STIGA.GO app
    did not propagate. The wire representation of 4 h is index 0, which
    proto3 omits. If the firmware sends a "rain touched" partial frame where
    rain[2] alone changed to 0, the rain submsg ends up empty on the wire
    (``[tag][length=0]``). decode_settings must still surface
    ``rain_sensor_delay_h = 4`` so the merge into ``live_settings`` overwrites
    a previous 8 h / 12 h value, instead of leaving the old reading sticky.
    """
    # Hand-craft the wire bytes: field 1 (rain submsg) as LEN with length 0.
    payload = _varint_bytes((1 << 3) | 2) + _varint_bytes(0)
    out = mm.decode_settings(payload)
    assert out["rain_sensor_enabled"] is False
    assert out["rain_sensor_delay_h"] == 4


def test_decode_settings_rain_delay_4h_with_enabled_present() -> None:
    """Rain submsg carries rain[1]=True but proto3-omits rain[2]=0 (= 4 h).
    Decoder must populate both fields so the merge actually moves the
    displayed delay back to 4 h instead of preserving the previous value.
    """
    payload = pb.encode({1: {1: 1}})  # rain submsg with enabled only
    out = mm.decode_settings(payload)
    assert out["rain_sensor_enabled"] is True
    assert out["rain_sensor_delay_h"] == 4


def test_decode_settings_rain_absent_clears_enabled_not_delay() -> None:
    """Absent rain submsg in a non-empty SETTINGS frame means rain is disabled.

    rain_sensor_enabled is emitted as False so the coordinator merge clears a
    previously-True value (e.g. set by the STIGA.GO app). rain_sensor_delay_h
    is intentionally NOT emitted: the delay falls back to the select entity's
    wire_default for display, but is not written into live_settings so a
    user-configured delay (e.g. 8 h) survives a disable/re-enable cycle from
    HA without being silently reset to 4 h every time a SETTINGS frame arrives
    without a rain sub-message.
    """
    payload = pb.encode({6: 1})  # only anti_theft, rain submsg absent
    out = mm.decode_settings(payload)
    assert out["rain_sensor_enabled"] is False
    assert "rain_sensor_delay_h" not in out


def test_decode_settings_real_mower_snapshot_after_obstacle_disable() -> None:
    """Captured 2026-05-18 from MAC 3C:22:7F:AA:BA:EA after publishing
    ``SETTINGS_UPDATE {15: {1: 0}}`` (obstacle_notifications=False, i.e. proto3
    default). The firmware's unsolicited LOG/SETTINGS reply is a sparse full
    snapshot — only ``cutting.zone_enabled=True`` was non-default. Locks in
    that the decoder correctly interprets rain-submsg absence as "rain off",
    instead of leaving the previous live_settings value intact.
    """
    payload = bytes.fromhex("22020801")
    assert mm.decode_settings(payload) == {
        "rain_sensor_enabled": False,
        "zone_cutting_height_enabled": True,
    }


def test_decode_settings_real_mower_snapshot_after_anti_theft_enable() -> None:
    """Same capture session, after ``SETTINGS_UPDATE {6: 1}`` (anti_theft=True).

    The snapshot now carries the touched field plus everything that was
    already non-default; rain is still absent (rain genuinely off on this
    robot), proving frames grow monotonically with each user change rather
    than shrinking to "only the touched field".
    """
    payload = bytes.fromhex("220208013001")
    assert mm.decode_settings(payload) == {
        "rain_sensor_enabled": False,
        "zone_cutting_height_enabled": True,
        "anti_theft": True,
    }


def test_decode_settings_unknown_cutting_height_index_returns_none() -> None:
    """Out-of-range index doesn't crash; key stays mapped to None."""
    payload = pb.encode({4: {2: 99}})
    out = mm.decode_settings(payload)
    assert out["cutting_height_mm"] is None


def test_decode_settings_empty_payload() -> None:
    assert mm.decode_settings(b"") == {}


# ---------------------------------------------------------------- encode_settings_update (wire-level)


def test_encode_settings_update_zone_cutting_height_enabled() -> None:
    """zone_cutting_height_enabled maps to cutting submsg field 4.1."""
    from custom_components.stiga_mower.mqtt_messages import encode_settings_update

    payload = encode_settings_update({"zone_cutting_height_enabled": True})
    decoded = pb.decode(payload)
    params = decoded[2]
    assert params[4][1] == 1


def test_encode_settings_update_rain_activate_8h_matches_capture() -> None:
    """Wire output for 'enable rain sensor at 8 h' must match the 2026-05-12
    app capture: params = {1: {1:1, 2:1}, 4: {1:1}}.
    """
    from custom_components.stiga_mower.mqtt_messages import encode_settings_update

    payload = encode_settings_update(
        {
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 8,
            "zone_cutting_height_enabled": True,
        }
    )
    decoded = pb.decode(payload)
    assert decoded[1] == 18
    params = decoded[2]
    assert params[1] == {1: 1, 2: 1}
    assert params[4] == {1: 1}
    assert decoded[3] == 18


def test_encode_settings_update_rain_activate_12h_matches_capture() -> None:
    """params = {1: {1:1, 2:2}, 4: {1:1}} for 12 h."""
    from custom_components.stiga_mower.mqtt_messages import encode_settings_update

    payload = encode_settings_update(
        {
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 12,
            "zone_cutting_height_enabled": True,
        }
    )
    params = pb.decode(payload)[2]
    assert params[1] == {1: 1, 2: 2}
    assert params[4] == {1: 1}


def test_encode_settings_update_rain_activate_4h_matches_capture() -> None:
    """params = {1: {1:1, 2:0}, 4: {1:1}} for 4 h (index 0, explicitly written)."""
    from custom_components.stiga_mower.mqtt_messages import encode_settings_update

    payload = encode_settings_update(
        {
            "rain_sensor_enabled": True,
            "rain_sensor_delay_h": 4,
            "zone_cutting_height_enabled": True,
        }
    )
    params = pb.decode(payload)[2]
    assert params[1] == {1: 1, 2: 0}
    assert params[4] == {1: 1}


def test_encode_settings_update_rain_disable_matches_capture() -> None:
    """Disabling: params = {1: {1:0}, 4: {1:1}} — no delay field."""
    from custom_components.stiga_mower.mqtt_messages import encode_settings_update

    payload = encode_settings_update(
        {
            "rain_sensor_enabled": False,
            "zone_cutting_height_enabled": True,
        }
    )
    params = pb.decode(payload)[2]
    assert params[1] == {1: 0}
    assert 2 not in params[1]
    assert params[4] == {1: 1}


# ---------------------------------------------------------------- decode_schedule


def test_decode_schedule_with_varint_blob() -> None:
    # Confirmed layout (Phase 6b): 7 days × 6 varint values.
    # All-zero blob = 42 bytes (all values 0, single byte each).
    bitmap = bytes(42)
    payload = pb.encode({1: 1, 2: bitmap, 4: 5})
    out = mm.decode_schedule(payload)
    assert out["enabled"] is True
    assert "days" in out
    assert len(out["days"]) == 7
    assert out["schedule_type"] == 5


def test_decode_schedule_with_short_blob() -> None:
    """Blobs shorter than 42 bytes are still parsed; missing days padded empty."""
    payload = pb.encode({1: 1, 2: b"\x00" * 13})
    out = mm.decode_schedule(payload)
    assert out["enabled"] is True
    assert len(out["days"]) == 7


def test_decode_schedule_disabled() -> None:
    payload = pb.encode({1: 0})
    assert mm.decode_schedule(payload) == {"enabled": False}


# ---------------------------------------------------------------- decode_base_status


def test_decode_base_status_full_frame() -> None:
    payload = pb.encode({1: 5, 4: 1, 10: 1})
    out = mm.decode_base_status(payload)
    assert out == {
        "status_type": "PUBLISHING_CORRECTIONS",
        "status_flag": "ACTIVE_OK",
        "led_mode": "always",
    }


def test_decode_base_status_unknown_codes_pass_through() -> None:
    payload = pb.encode({1: 99, 4: 99, 10: 99})
    out = mm.decode_base_status(payload)
    assert out == {"status_type": 99, "status_flag": 99, "led_mode": 99}


def test_decode_base_status_with_location_and_network() -> None:
    """field 8 = location, field 9.3 = network (same wrap as robot 20.3)."""
    payload = pb.encode(
        {
            1: 5,  # PUBLISHING_CORRECTIONS
            4: 1,  # ACTIVE_OK
            8: {1: 0, 2: 14, 5: 95},  # coverage GOOD, 14 sats, RTK 95%
            9: {3: {4: 26201, 5: "LTE", 6: 20, 7: -73, 10: -94, 11: 72, 12: -10}},
            10: 2,  # scheduled LED
        }
    )
    out = mm.decode_base_status(payload)
    assert out["status_type"] == "PUBLISHING_CORRECTIONS"
    assert out["status_flag"] == "ACTIVE_OK"
    assert out["led_mode"] == "scheduled"
    assert out["gps_quality"] == "GOOD"
    assert out["satellites"] == 14
    assert out["rtk_quality_pct"] == 95
    assert out["network_kind"] == 26201
    assert out["network_type"] == "LTE"
    assert out["network_band"] == 20
    assert out["rssi"] == -73
    assert out["rsrp"] == -94
    assert out["signal_quality_pct"] == 72
    assert out["rsrq"] == -10


def test_decode_base_status_signal_quality_sentinel_dropped() -> None:
    payload = pb.encode({9: {3: {10: -90, 11: -32768, 12: -8}}})
    out = mm.decode_base_status(payload)
    assert out["rsrp"] == -90
    assert out["rsrq"] == -8
    assert "signal_quality_pct" not in out


def test_decode_base_status_empty_payload() -> None:
    assert mm.decode_base_status(b"") == {}


# ---------------------------------------------------------------- decode_base_version


def test_decode_base_version_full_frame() -> None:
    payload = pb.encode({1: b"\x00\x00\x05", 2: b"\x01\x02\x03", 3: b"\x10", 5: "LTE-M", 6: "EU"})
    out = mm.decode_base_version(payload)
    assert out == {
        "hardware": "0.0.5",
        "firmware": "1.2.3",
        "build": "16",
        "modem": "LTE-M",
        "localization": "EU",
    }


def test_decode_base_version_empty_payload() -> None:
    assert mm.decode_base_version(b"") == {}


# ---------------------------------------------------------------- decode_notification


def test_decode_notification_valid_json() -> None:
    body = {"title": "Mower stuck", "data": {"type": "blocked_error"}}
    payload = json.dumps(body).encode()
    assert mm.decode_notification(payload) == body


def test_decode_notification_invalid_json_returns_empty() -> None:
    assert mm.decode_notification(b"not-json{") == {}


# ---------------------------------------------------------------- decode_command_ack


def test_decode_command_ack_ok() -> None:
    payload = pb.encode({1: mc.ROBOT_CMD_START, 2: 1})
    out = mm.decode_command_ack(payload)
    assert out == {
        "cmd_type": mc.ROBOT_CMD_START,
        "cmd_name": "START",
        "result": 1,
        "ok": True,
    }


def test_decode_command_ack_failure() -> None:
    payload = pb.encode({1: mc.ROBOT_CMD_START, 2: 7})
    out = mm.decode_command_ack(payload)
    assert out["ok"] is False
    assert out["result"] == 7


def test_decode_command_ack_unknown_cmd() -> None:
    payload = pb.encode({1: 199, 2: 1})
    out = mm.decode_command_ack(payload)
    assert out["cmd_type"] == 199
    assert out["cmd_name"] is None
    assert out["ok"] is True


# ---------------------------------------------------------------- encode_command


def test_encode_command_includes_echo_field() -> None:
    """matthewgream's encoder duplicates the cmd_id in field 3."""
    encoded = mm.encode_command(mc.ROBOT_CMD_START)
    # {1: 1, 3: 1} -> 08 01 18 01
    assert encoded.hex() == "08011801"


def test_encode_command_with_params_nests_in_field_2() -> None:
    encoded = mm.encode_command(mc.ROBOT_CMD_STATUS_REQUEST, {1: 1})
    # {1: 28, 2: {1: 1}, 3: 28} -> 08 1c 12 02 08 01 18 1c
    assert encoded.hex() == "081c12020801181c"


def test_encode_command_rejects_unknown_id() -> None:
    with pytest.raises(ValueError, match="unknown robot command"):
        mm.encode_command(123, None)


def test_encode_status_request_default_includes_all_subframes() -> None:
    encoded = mm.encode_status_request()
    # All four subframe flags set: {1: 28, 2: {1:1, 2:1, 3:1, 4:1}, 3: 28}
    expected = pb.encode({1: 28, 2: {1: 1, 2: 1, 3: 1, 4: 1}, 3: 28})
    assert encoded == expected


def test_encode_status_request_can_request_battery_only() -> None:
    encoded = mm.encode_status_request(
        battery=True,
        mowing=False,
        location=False,
        network=False,
    )
    expected = pb.encode({1: 28, 2: {1: 1}, 3: 28})
    assert encoded == expected


def test_encode_status_request_with_no_flags_omits_param_field() -> None:
    """If the caller asks for nothing, send a parameterless STATUS_REQUEST.

    Matches matthewgream's `encodeRobotStatusRequestTypes`: when every flag
    is false the params dict is empty and the encoder skips field 2.
    """
    encoded = mm.encode_status_request(
        battery=False,
        mowing=False,
        location=False,
        network=False,
    )
    expected = pb.encode({1: 28, 3: 28})
    assert encoded == expected


def test_encode_simple_request_settings() -> None:
    encoded = mm.encode_simple_request(mc.ROBOT_CMD_SETTINGS_REQUEST)
    expected = pb.encode({1: 17, 3: 17})
    assert encoded == expected


def test_encode_reset_error_matches_app_capture() -> None:
    # Frame captured 2026-05-03 from the official STIGA.GO app pressing
    # "Reset error" (capture_app_trace.jsonl). Robot ACKed with result=1.
    encoded = mm.encode_simple_request(mc.ROBOT_CMD_RESET_ERROR)
    assert encoded.hex() == "08251825"


# ---------------------------------------------------------------- encode_settings_update (single-key)


def test_encode_settings_update_rain_sensor_enabled() -> None:
    payload = mm.encode_settings_update({"rain_sensor_enabled": True})
    decoded = pb.decode(payload)
    # Field 1 = cmd_id (18), field 2 = params, field 3 = echo
    assert decoded[1] == 18
    params = decoded[2]
    assert isinstance(params, dict)
    assert params[1][1] == 1  # rain.enabled = True


def test_encode_settings_update_cutting_height_40mm() -> None:
    payload = mm.encode_settings_update({"cutting_height_mm": 40})
    decoded = pb.decode(payload)
    params = decoded[2]
    # 40mm -> index 4
    assert params[4][2] == 4


def test_encode_settings_update_anti_theft() -> None:
    payload = mm.encode_settings_update({"anti_theft": False})
    decoded = pb.decode(payload)
    params = decoded[2]
    assert params[6] == 0


def test_encode_settings_update_rain_delay_8h() -> None:
    payload = mm.encode_settings_update({"rain_sensor_delay_h": 8})
    decoded = pb.decode(payload)
    params = decoded[2]
    # 8h -> index 1
    assert params[1][2] == 1


def test_encode_settings_update_unknown_cutting_height_skipped() -> None:
    # 37mm is not a valid height — should not include cutting field
    payload = mm.encode_settings_update({"cutting_height_mm": 37})
    decoded = pb.decode(payload)
    params = decoded.get(2)
    # params may be None or not contain field 4
    if params is not None:
        assert 4 not in params


def test_encode_settings_update_multiple_fields() -> None:
    payload = mm.encode_settings_update(
        {
            "rain_sensor_enabled": True,
            "keyboard_lock": False,
            "cutting_height_mm": 30,
        }
    )
    decoded = pb.decode(payload)
    params = decoded[2]
    assert params[1][1] == 1  # rain on
    assert params[2] == 0  # keyboard_lock off
    assert params[4][2] == 2  # 30mm -> index 2
