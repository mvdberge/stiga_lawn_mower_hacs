"""Tests for the MQTT message decoders/encoders.

These exercise the pure decoder functions: bytes-in → dict-out. We build
the protobuf inputs through ``protobuf_codec.encode`` (already covered by
``test_protobuf_codec.py``) plus a small ``_fixed64`` helper for the GPS
fields, since the codec only encodes FIXED32 floats out of the box.
"""

from __future__ import annotations

import json
import struct

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


def _fixed64(field: int, value: float) -> bytes:
    """Encode a single FIXED64 field as a protobuf wire-format fragment."""
    return _varint_bytes((field << 3) | 1) + struct.pack("<d", value)


def _wrap_len(field: int, payload: bytes) -> bytes:
    """Wrap a raw byte payload as a LEN-delimited field."""
    return _varint_bytes((field << 3) | 2) + _varint_bytes(len(payload)) + payload


# ---------------------------------------------------------------- decode_status


def test_decode_status_full_frame() -> None:
    """A frame with every documented field roundtrips to descriptive keys."""
    # Build via the codec for VARINT + nested dicts; FIXED64 fields appended
    # manually because the codec doesn't emit FIXED64 (decoder leaves them raw).
    body = pb.encode(
        {
            1: 1,  # status_valid
            2: 1,  # operable
            3: 32,  # CUTTING_BORDER
            4: {1: 2, 2: 22},  # status_error
            10: {1: 0x01A9, 2: 0, 3: 0, 4: 0},  # info_code: RAIN_SENSOR
            13: 0,  # not docking
            17: {1: 5000, 2: 87},  # battery
            18: {1: 3, 2: 42, 3: 78},  # mowing
            20: {3: {4: 5, 5: 9, 6: 3, 7: -65, 10: -90, 11: 73, 12: -10}},
        }
    )
    # location subfield needs FIXED64 doubles for fields 3 + 4
    location_inner = pb.encode({1: 0, 2: 14, 5: 95})
    location_inner += _fixed64(3, 123.4)  # lat offset cm
    location_inner += _fixed64(4, -56.7)  # lon offset cm
    payload = body + _wrap_len(19, location_inner)

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
    assert out["battery_level"] == 87
    assert out["current_zone"] == 3
    assert out["zone_completed_pct"] == 42
    assert out["garden_completed_pct"] == 78
    assert out["gps_quality"] == "GOOD"
    assert out["satellites"] == 14
    assert out["lat_offset_cm"] == pytest.approx(123.4)
    assert out["lon_offset_cm"] == pytest.approx(-56.7)
    assert out["rtk_quality_pct"] == 95
    assert out["rssi"] == -65
    assert out["rsrp"] == -90
    assert out["rsrq"] == -10
    assert out["signal_quality_pct"] == 73


def test_decode_status_minimal_frame() -> None:
    """Mowers in early-init can omit nested groups; we keep keys absent."""
    payload = pb.encode({1: 1, 3: 4})  # status_valid + DOCKED
    out = mm.decode_status(payload)
    assert out == {"status_valid": True, "status_type": "DOCKED"}


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


# ---------------------------------------------------------------- decode_position


def test_decode_position_full_frame() -> None:
    payload = _fixed64(1, 12.34) + _fixed64(2, -56.78) + _fixed64(3, 1.5708)
    out = mm.decode_position(payload)
    assert out["lon_offset_m"] == pytest.approx(12.34)
    assert out["lat_offset_m"] == pytest.approx(-56.78)
    assert out["orientation_rad"] == pytest.approx(1.5708)


def test_decode_position_missing_fields() -> None:
    # only longitude
    payload = _fixed64(1, 1.0)
    out = mm.decode_position(payload)
    assert out == {"lon_offset_m": 1.0}


def test_decode_position_empty_payload() -> None:
    assert mm.decode_position(b"") == {}


# ---------------------------------------------------------------- decode_settings


def test_decode_settings_full_frame() -> None:
    payload = pb.encode(
        {
            1: {1: 1, 2: 1},  # rain sensor on, 8h delay
            2: 0,  # keyboard lock off
            4: {1: 1, 2: 5},  # zone height enabled, 45 mm
            6: 1,  # anti-theft on
            7: 0,  # smart cut height off
            8: {1: 1, 3: 2},  # long exit on, mode 2
            9: 1,  # uniform height
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
        "long_exit_mode": 2,
        "zone_cutting_height_uniform": True,
        "push_notifications": True,
        "obstacle_notifications": False,
    }


def test_decode_settings_unknown_cutting_height_index_returns_none() -> None:
    """Out-of-range index doesn't crash; key stays mapped to None."""
    payload = pb.encode({4: {2: 99}})
    out = mm.decode_settings(payload)
    assert out["cutting_height_mm"] is None


def test_decode_settings_empty_payload() -> None:
    assert mm.decode_settings(b"") == {}


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
