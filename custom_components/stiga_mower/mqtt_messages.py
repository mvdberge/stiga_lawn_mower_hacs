"""Decode and encode helpers for STIGA MQTT message payloads.

These are pure stateless functions: bytes in, dict out (and vice versa).
Keeping them out of `mqtt_client.py` lets us unit-test the wire-level
parsing without spinning up an aiomqtt connection.

All field numbers and enum mappings come from `mqtt_constants.py`. When a
frame field is missing or malformed, the corresponding key in the returned
dict is left absent rather than set to ``None``; callers can then reuse a
previous value or surface the entity as unavailable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import mqtt_constants as mc
from . import protobuf_codec as pb

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------- Robot status


def decode_status(payload: bytes) -> dict[str, Any]:
    """Parse a `{mac}/LOG/STATUS` protobuf frame into a flat dict.

    The returned mapping uses descriptive keys (``status_type``,
    ``info_code``, ``current_zone`` …) instead of raw field numbers so the
    coordinator can treat it as the single source of truth without knowing
    the wire layout.
    """
    try:
        raw = pb.decode(payload)
    except pb.ProtobufError as err:
        _LOGGER.warning("STATUS frame decode failed: %s", err)
        return {}

    out: dict[str, Any] = {}
    _set_if_present(out, "status_valid", raw, 1, _as_bool)
    _set_if_present(out, "operable", raw, 2, _as_bool)
    _set_if_present(out, "status_type", raw, 3, lambda v: mc.ROBOT_STATUS_TYPES.get(v, v))

    if isinstance(error := raw.get(4), dict):
        _set_if_present(out, "error_code1", error, 1)
        _set_if_present(out, "error_code2", error, 2)

    info = raw.get(10) if isinstance(raw.get(10), dict) else None
    if info is not None and (code := info.get(1)) is not None:
        out["info_code"] = code
        out["info_label"] = mc.ROBOT_STATUS_INFO_CODES.get(code)
        if (sensor := mc.ROBOT_INFO_CODE_TO_SENSOR.get(code)) is not None:
            out["info_sensor"] = sensor

    _set_if_present(out, "docking", raw, 13, _as_bool)

    if isinstance(battery := raw.get(17), dict):
        _set_if_present(out, "battery_capacity_mah", battery, 1)
        _set_if_present(out, "battery_level", battery, 2)
        # 17.7 = battery temperature °C (float), 17.9 = total work time (minutes),
        # 17.12 = battery current A (negative = discharging)
        if (temp := battery.get(7)) is not None and isinstance(temp, float):
            out["battery_temp_c"] = round(temp, 1)
        _set_if_present(out, "total_work_time", battery, 9)
        if (current := battery.get(12)) is not None and isinstance(current, float):
            out["battery_current"] = round(current, 3)

    if isinstance(mowing := raw.get(18), dict):
        _set_if_present(out, "current_zone", mowing, 1)
        _set_if_present(out, "zone_completed_pct", mowing, 2)
        _set_if_present(out, "garden_completed_pct", mowing, 3)
        # 18.4 = battery detail sub-message: {1: level%, 2: voltage V, 3: charging bool}
        if isinstance(batt_detail := mowing.get(4), dict):
            _set_if_present(out, "battery_level", batt_detail, 1)
            if (voltage := batt_detail.get(2)) is not None and isinstance(voltage, float):
                out["battery_voltage"] = round(voltage, 2)
            if (charging := batt_detail.get(3)) is not None:
                out["battery_charging"] = bool(charging)

    if isinstance(location := raw.get(19), dict):
        # 19.1 = gps_quality enum (absent = implicitly GOOD on this firmware)
        _set_if_present(
            out,
            "gps_quality",
            location,
            1,
            lambda v: mc.ROBOT_GPS_QUALITY.get(v, v),
        )
        _set_if_present(out, "satellites", location, 2)
        # 19.3 and 19.4 are accuracy/dilution metrics, NOT position offsets.
        # Position comes from the separate ROBOT_POSITION topic.
        # 19.6 = RTK fix type (4 = RTK fixed)
        _set_if_present(out, "rtk_fix_type", location, 6)

    network = raw.get(20)
    if isinstance(network, dict) and isinstance(network.get(3), dict):
        sub = network[3]
        _set_if_present(out, "network_kind", sub, 4)
        _set_if_present(out, "network_type", sub, 5)
        _set_if_present(out, "network_band", sub, 6)
        # 20.3.10 = rsrp, 20.3.11 = rssi (-32768 = modem sentinel for unavailable),
        # 20.3.12 = rsrq
        _set_if_present(out, "rsrp", sub, 10, _as_signed_int32)
        rssi = _as_signed_int32(sub[11]) if 11 in sub else None
        if rssi is not None and rssi != -32768:
            out["rssi"] = rssi
        _set_if_present(out, "rsrq", sub, 12, _as_signed_int32)

    return out


# ---------------------------------------------------------------- Robot position


def decode_position(payload: bytes) -> dict[str, Any]:
    """Parse a `{mac}/LOG/ROBOT_POSITION` frame.

    Field 1 is the longitude offset in metres, field 2 the latitude offset,
    field 3 the orientation in radians. All three are 8-byte little-endian
    IEEE 754 doubles. Offsets are relative to the docking station.
    """
    try:
        raw = pb.decode(payload)
    except pb.ProtobufError as err:
        _LOGGER.warning("POSITION frame decode failed: %s", err)
        return {}

    out: dict[str, Any] = {}
    if (lon := pb.read_double_le(raw.get(1))) is not None:
        out["lon_offset_m"] = lon
    if (lat := pb.read_double_le(raw.get(2))) is not None:
        out["lat_offset_m"] = lat
    if (orient := pb.read_double_le(raw.get(3))) is not None:
        out["orientation_rad"] = orient
    return out


# ---------------------------------------------------------------- Robot settings


def decode_settings(payload: bytes) -> dict[str, Any]:
    """Parse a `{mac}/LOG/SETTINGS` frame into a flat dict.

    Mirrors `decodeRobotSettings` from matthewgream/stiga-api. All keys
    track the on/off state, plus the two enum mappings (``rain_sensor_delay``
    and ``cutting_height``) are translated back to human values (hours / mm).
    """
    try:
        raw = pb.decode(payload)
    except pb.ProtobufError as err:
        _LOGGER.warning("SETTINGS frame decode failed: %s", err)
        return {}

    out: dict[str, Any] = {}
    rain = raw.get(1) if isinstance(raw.get(1), dict) else None
    if rain is not None:
        if rain.get(1) is not None:
            out["rain_sensor_enabled"] = bool(rain[1])
        if rain.get(2) is not None:
            out["rain_sensor_delay_h"] = mc.RAIN_DELAY_INDEX_TO_HOURS.get(rain[2])

    if raw.get(2) is not None:
        out["keyboard_lock"] = bool(raw[2])

    cutting = raw.get(4) if isinstance(raw.get(4), dict) else None
    if cutting is not None:
        if cutting.get(1) is not None:
            out["zone_cutting_height_enabled"] = bool(cutting[1])
        if cutting.get(2) is not None:
            out["cutting_height_mm"] = mc.CUTTING_HEIGHT_INDEX_TO_MM.get(cutting[2])

    if raw.get(6) is not None:
        out["anti_theft"] = bool(raw[6])
    if raw.get(7) is not None:
        out["smart_cutting_height"] = bool(raw[7])

    long_exit = raw.get(8) if isinstance(raw.get(8), dict) else None
    if long_exit is not None:
        if long_exit.get(1) is not None:
            out["long_exit"] = bool(long_exit[1])
        if long_exit.get(3) is not None:
            out["long_exit_mode"] = long_exit[3]

    if raw.get(9) is not None:
        out["zone_cutting_height_uniform"] = bool(raw[9])

    push = raw.get(14) if isinstance(raw.get(14), dict) else None
    if push is not None and push.get(1) is not None:
        out["push_notifications"] = bool(push[1])

    obstacle = raw.get(15) if isinstance(raw.get(15), dict) else None
    if obstacle is not None and obstacle.get(1) is not None:
        out["obstacle_notifications"] = bool(obstacle[1])

    return out


# ---------------------------------------------------------------- Schedule


def decode_schedule(payload: bytes) -> dict[str, Any]:
    """Parse a `{mac}/LOG/SCHEDULING_SETTINGS` frame.

    Confirmed wire layout for Vista/A15v robots (Phase 6a/6b, 2026-04-28):
    - Field 1: global enabled flag (varint bool).
    - Field 2: schedule blob — 7 days × 6 varint-encoded bitmap values.
      Each varint encodes one byte of the 48-slot bitmap (30-min slots,
      bit 0 of value 0 = 00:00).  The varints are written without field
      tags — the blob is a raw sequence of 42 varints (6 per day × 7 days).
      Values ≤ 127 occupy 1 byte; values 128–255 occupy 2 bytes (e.g.
      0xC0=192 → 0xC0 0x01, 0xE3=227 → 0xE3 0x01), which explains the
      56-byte blob observed on hardware that uses bitmask values > 127.
    - Field 4: schedule type (opaque int, typically 5).

    Note — A-Series (classic) robots (matthewgream reference):
      Field 2 contains 42 raw bytes (7 × 6), not varints.  Values ≤ 127
      are wire-identical to single-byte varints, so this decoder handles
      both formats transparently for sparse schedules.  Densely-packed
      A-Series schedules with bytes > 127 would be misread, but such
      hardware is not supported by this integration.

    Returns:
      ``enabled``       — scheduling active globally (field 1 bool)
      ``schedule_type`` — opaque type int (field 4)
      ``days``          — list of 7 dicts, one per weekday (Mon=0 … Sun=6):
                          ``{"slots": set[int]}``
                          where each slot index maps to ``slot * 30`` minutes.
    """
    try:
        raw = pb.decode(payload)
    except pb.ProtobufError as err:
        _LOGGER.warning("SCHEDULE frame decode failed: %s", err)
        return {}

    out: dict[str, Any] = {}
    if raw.get(1) is not None:
        out["enabled"] = bool(raw[1])

    blob = raw.get(2)
    if isinstance(blob, str):
        blob = blob.encode("latin-1")
    if isinstance(blob, (bytes, bytearray)) and blob:
        out["days"] = unpack_schedule(bytes(blob))

    if raw.get(4) is not None:
        out["schedule_type"] = raw[4]

    return out


def unpack_schedule(blob: bytes) -> list[dict[str, Any]]:
    """Decode the schedule blob into a list of 7 day-dicts.

    The blob is a flat sequence of 42 varint-encoded values (6 per day × 7
    days).  Each value is one byte of the 48-slot bitmap for that day: bit N
    of value M = slot (M*8 + N), active when set.  Slot index maps to wall
    time as ``slot * 30`` minutes from midnight.

    Returns 7 dicts with key ``slots`` (set[int]).  Missing days are padded
    with empty slot sets.
    """
    # Decode all varints from the blob
    bitmap_vals: list[int] = []
    pos = 0
    while pos < len(blob) and len(bitmap_vals) < mc.SCHEDULE_DAYS * mc.SCHEDULE_TIME_BYTES:
        val = 0
        shift = 0
        while pos < len(blob):
            b = blob[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        bitmap_vals.append(val)

    days: list[dict[str, Any]] = []
    for d in range(mc.SCHEDULE_DAYS):
        base = d * mc.SCHEDULE_TIME_BYTES
        slots: set[int] = set()
        for byte_i in range(mc.SCHEDULE_TIME_BYTES):
            idx = base + byte_i
            if idx >= len(bitmap_vals):
                break
            val = bitmap_vals[idx]
            for bit in range(8):
                if val & (1 << bit):
                    slots.add(byte_i * 8 + bit)
        days.append({"slots": slots})
    return days


def pack_schedule(days: list[dict[str, Any]]) -> bytes:
    """Encode a list of 7 day-dicts back into the schedule blob.

    Inverse of :func:`unpack_schedule`.  Each day dict must have:
      ``slots`` (set[int]) — active half-hour slot indices (0–47)

    The output is a flat sequence of 42 varint-encoded bitmap values
    (6 per day × 7 days), matching the wire format observed on hardware.
    """

    def _write_varint(n: int) -> bytes:
        out: list[int] = []
        while n > 0x7F:
            out.append((n & 0x7F) | 0x80)
            n >>= 7
        out.append(n)
        return bytes(out)

    blob = bytearray()
    for d in range(mc.SCHEDULE_DAYS):
        day = days[d] if d < len(days) else {}
        bitmap = [0] * mc.SCHEDULE_TIME_BYTES
        for slot in day.get("slots", set()):
            if 0 <= slot < mc.SCHEDULE_SLOTS_PER_DAY:
                byte_i, bit = divmod(slot, 8)
                bitmap[byte_i] |= 1 << bit
        for val in bitmap:
            blob += _write_varint(val)
    return bytes(blob)


# ---------------------------------------------------------------- Base status


def decode_base_status(payload: bytes) -> dict[str, Any]:
    """Parse a base-station `{base_mac}/LOG/STATUS` frame."""
    try:
        raw = pb.decode(payload)
    except pb.ProtobufError as err:
        _LOGGER.warning("BASE STATUS frame decode failed: %s", err)
        return {}

    out: dict[str, Any] = {}
    _set_if_present(
        out,
        "status_type",
        raw,
        1,
        lambda v: mc.BASE_STATUS_TYPES.get(v, v),
    )
    _set_if_present(
        out,
        "status_flag",
        raw,
        4,
        lambda v: mc.BASE_STATUS_FLAGS.get(v, v),
    )
    _set_if_present(
        out,
        "led_mode",
        raw,
        10,
        lambda v: mc.BASE_LED_MODE_INDEX_TO_NAME.get(v, v),
    )
    return out


# ---------------------------------------------------------------- Notifications


def decode_notification(payload: bytes) -> dict[str, Any]:
    """Parse a `{mac}/JSON_NOTIFICATION` payload.

    Unlike the LOG/* topics, notifications are JSON-encoded — likely a
    direct passthrough of the cloud's push-notification record.
    """
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError) as err:
        _LOGGER.warning("JSON_NOTIFICATION decode failed: %s", err)
        return {}


# ---------------------------------------------------------------- Command ACK


def decode_command_ack(payload: bytes) -> dict[str, Any]:
    """Parse a `CMD_ROBOT_ACK/{mac}` frame: `{1: cmd_type, 2: result}`."""
    try:
        raw = pb.decode(payload)
    except pb.ProtobufError as err:
        _LOGGER.warning("CMD_ROBOT_ACK decode failed: %s", err)
        return {}

    out: dict[str, Any] = {}
    if (cmd_type := raw.get(1)) is not None:
        out["cmd_type"] = cmd_type
        out["cmd_name"] = mc.ROBOT_CMD_NAMES.get(cmd_type)
    if (result := raw.get(2)) is not None:
        out["result"] = result
        out["ok"] = result == mc.ROBOT_CMD_ACK_OK
    return out


# ---------------------------------------------------------------- Command encoding


def encode_command(cmd_id: int, params: dict[int, Any] | None = None) -> bytes:
    """Build a `{mac}/CMD_ROBOT` frame: `{1: cmd_id, 2: params, 3: cmd_id}`.

    The duplicated field 3 ('echo') matches the official app's encoding —
    the broker rejects frames that omit it.
    """
    if cmd_id not in mc.ROBOT_CMD_NAMES:
        raise ValueError(f"unknown robot command id {cmd_id!r}")
    return pb.encode({1: cmd_id, 2: params, 3: cmd_id})


def encode_status_request(
    *,
    battery: bool = True,
    mowing: bool = True,
    location: bool = True,
    network: bool = True,
) -> bytes:
    """Build a STATUS_REQUEST (cmd 28) payload.

    The request body lets the caller scope which sub-frames the mower
    should emit — for a full refresh, leave all four flags at their
    default ``True``.
    """
    selected: dict[int, Any] = {}
    if battery:
        selected[1] = 1
    if mowing:
        selected[2] = 1
    if location:
        selected[3] = 1
    if network:
        selected[4] = 1
    return encode_command(mc.ROBOT_CMD_STATUS_REQUEST, selected or None)


def encode_simple_request(cmd_id: int) -> bytes:
    """Encode a parameterless request (settings, scheduling, version, position)."""
    return encode_command(cmd_id, None)


def encode_settings_update(settings: dict[str, Any]) -> bytes:
    """Build a SETTINGS_UPDATE (cmd 18) payload from human-readable settings keys.

    Supported keys (mirroring decode_settings output):
      - ``rain_sensor_enabled``  (bool) → field 1.1
      - ``rain_sensor_delay_h``  (int hours: 4/8/12) → field 1.2
      - ``keyboard_lock``        (bool) → field 2
      - ``cutting_height_mm``    (int mm: 20–60 step 5) → field 4.2
      - ``anti_theft``           (bool) → field 6
      - ``smart_cutting_height`` (bool) → field 7
      - ``long_exit``            (bool) → field 8.1
      - ``push_notifications``   (bool) → field 14.1
      - ``obstacle_notifications``(bool) → field 15.1
    """
    params: dict[int, Any] = {}

    rain: dict[int, Any] = {}
    if (v := settings.get("rain_sensor_enabled")) is not None:
        rain[1] = int(bool(v))
    if (delay_h := settings.get("rain_sensor_delay_h")) is not None:
        idx = mc.RAIN_DELAYS_HOURS.get(int(delay_h))
        if idx is not None:
            rain[2] = idx
    if rain:
        params[1] = rain

    if (v := settings.get("keyboard_lock")) is not None:
        params[2] = int(bool(v))

    cutting: dict[int, Any] = {}
    if (height_mm := settings.get("cutting_height_mm")) is not None:
        idx = mc.CUTTING_HEIGHTS_MM.get(int(height_mm))
        if idx is not None:
            cutting[2] = idx
    if cutting:
        params[4] = cutting

    if (v := settings.get("anti_theft")) is not None:
        params[6] = int(bool(v))
    if (v := settings.get("smart_cutting_height")) is not None:
        params[7] = int(bool(v))

    long_exit: dict[int, Any] = {}
    if (v := settings.get("long_exit")) is not None:
        long_exit[1] = int(bool(v))
    if long_exit:
        params[8] = long_exit

    push: dict[int, Any] = {}
    if (v := settings.get("push_notifications")) is not None:
        push[1] = int(bool(v))
    if push:
        params[14] = push

    obstacle: dict[int, Any] = {}
    if (v := settings.get("obstacle_notifications")) is not None:
        obstacle[1] = int(bool(v))
    if obstacle:
        params[15] = obstacle

    return encode_command(mc.ROBOT_CMD_SETTINGS_UPDATE, params or None)


# ---------------------------------------------------------------- Helpers


def _set_if_present(
    out: dict[str, Any],
    key: str,
    src: dict[int, Any],
    field: int,
    transform: Any = None,
) -> None:
    value = src.get(field)
    if value is None:
        return
    out[key] = transform(value) if transform is not None else value


def _as_bool(value: Any) -> bool:
    return bool(value)


def _as_signed_int32(value: int) -> int:
    """Reinterpret a raw protobuf VARINT as a signed 32-bit integer.

    Protobuf encodes negative int32 fields as large uint32 values
    (e.g. -93 → 4,294,967,203).  Mirrors matthewgream's `toInt32`.
    """
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return value
