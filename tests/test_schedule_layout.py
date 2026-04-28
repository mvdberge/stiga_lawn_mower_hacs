"""Phase 6a/6b: schedule wire-format round-trip tests.

Layout confirmed by live capture 2026-04-28:
  Field 2 = flat sequence of varint-encoded bitmap values.
  7 days × 6 varints per day = 42 varints total.
  Each varint encodes one byte of the 48-slot daily bitmap (30-min slots,
  bit 0 of varint 0 = slot 0 = 00:00).

Live captured blob (56 wire bytes because bitmask values > 127 need 2 bytes):
  0000c001e3010100  × 7  (decoded to 6 ints: [0, 0, 192, 227, 1, 0] per day)
  → active slots per day: 22(11:00), 23(11:30), 24(12:00), 25(12:30),
                           29(14:30), 30(15:00), 31(15:30), 32(16:00)
  → windows: 11:00–13:00 and 14:30–16:30
"""

from __future__ import annotations

import pytest

from custom_components.stiga_mower import mqtt_constants as mc
from custom_components.stiga_mower.mqtt_messages import (
    decode_schedule,
    pack_schedule,
    unpack_schedule,
)

# ------------------------------------------------------------------ constants

# One day's varint-encoded bitmap: [0, 0, 192, 227, 1, 0]
# 192 = 0xC0 → wire bytes 0xC0 0x01 (2 bytes varint)
# 227 = 0xE3 → wire bytes 0xE3 0x01 (2 bytes varint)
LIVE_DAY_WIRE = bytes.fromhex("0000c001e3010100")  # 8 wire bytes for 6 values
LIVE_BLOB = LIVE_DAY_WIRE * 7  # 56 wire bytes total

LIVE_ACTIVE_SLOTS = {22, 23, 24, 25, 29, 30, 31, 32}

# Minimal outer frame: field 1 varint 1 (enabled) + field 2 LEN carrying LIVE_BLOB
# field 1: tag=0x08, value=0x01
# field 2: tag=0x12, length varint (56=0x38), then 56 bytes
LIVE_FRAME = bytes([0x08, 0x01, 0x12, 0x38]) + LIVE_BLOB

# All-zero blob: 7 days × 6 varints of value 0 = 42 wire bytes (all single-byte)
EMPTY_BLOB = bytes(7 * 6)


# ------------------------------------------------------------------ unpack_schedule


def test_unpack_live_capture_returns_7_days() -> None:
    days = unpack_schedule(LIVE_BLOB)
    assert len(days) == 7


def test_unpack_live_capture_correct_slots() -> None:
    days = unpack_schedule(LIVE_BLOB)
    for d in days:
        assert d["slots"] == LIVE_ACTIVE_SLOTS


def test_unpack_live_capture_matches_user_schedule() -> None:
    """11:00–13:00 and 14:30–16:30 every day."""
    days = unpack_schedule(LIVE_BLOB)
    for d in days:
        slots = d["slots"]
        # 11:00–13:00: slots 22,23,24,25
        assert {22, 23, 24, 25}.issubset(slots)
        # 14:30–16:30: slots 29,30,31,32
        assert {29, 30, 31, 32}.issubset(slots)
        # nothing outside those windows
        assert slots == LIVE_ACTIVE_SLOTS


def test_unpack_slot_zero_is_midnight() -> None:
    # blob with only varint 0x01 (value=1, bit 0) for day 0
    blob = bytes([0x01]) + bytes(5) + bytes(6 * 6)  # day0 byte0=1, rest 0
    days = unpack_schedule(blob)
    assert 0 in days[0]["slots"]


def test_unpack_slot_47_is_2330() -> None:
    # slot 47 = byte_i=5, bit=7 → value = 0x80 = 128 → varint 0x80 0x01
    blob = bytes(5) + bytes([0x80, 0x01]) + bytes(6 * 6)
    days = unpack_schedule(blob)
    assert 47 in days[0]["slots"]


def test_unpack_empty_blob_returns_7_empty_days() -> None:
    days = unpack_schedule(EMPTY_BLOB)
    assert len(days) == 7
    for d in days:
        assert d["slots"] == set()


def test_unpack_short_blob_pads_missing_days() -> None:
    # Only 1 day provided (6 varint bytes, all 0)
    days = unpack_schedule(bytes(6))
    assert len(days) == 7
    for d in days:
        assert d["slots"] == set()


def test_unpack_each_day_is_independent() -> None:
    # day 2 (Wed): slot 0 only — first varint = 0x01
    blob = bytes(12) + bytes([0x01]) + bytes(5) + bytes(4 * 6)
    days = unpack_schedule(blob)
    assert 0 in days[2]["slots"]
    assert days[0]["slots"] == set()
    assert days[1]["slots"] == set()


# ------------------------------------------------------------------ pack_schedule


def test_pack_roundtrip_live_capture() -> None:
    original = unpack_schedule(LIVE_BLOB)
    repacked = pack_schedule(original)
    # Re-unpack and compare slots (wire bytes may differ in varint length)
    assert unpack_schedule(repacked) == original


def test_pack_all_off_produces_minimal_blob() -> None:
    days = [{"slots": set()} for _ in range(7)]
    blob = pack_schedule(days)
    assert unpack_schedule(blob) == days


def test_pack_single_slot_roundtrip() -> None:
    days = [{"slots": {0}} if i == 0 else {"slots": set()} for i in range(7)]
    blob = pack_schedule(days)
    back = unpack_schedule(blob)
    assert back[0]["slots"] == {0}
    assert back[1]["slots"] == set()


def test_pack_ignores_out_of_range_slots() -> None:
    days = [{"slots": {48, 99}} for _ in range(7)]
    blob = pack_schedule(days)
    back = unpack_schedule(blob)
    for d in back:
        assert d["slots"] == set()


def test_pack_produces_7_days() -> None:
    days = [{"slots": {i}} for i in range(7)]
    blob = pack_schedule(days)
    back = unpack_schedule(blob)
    assert len(back) == 7


def test_pack_all_slots_roundtrip() -> None:
    all_slots = set(range(48))
    days = [{"slots": all_slots} for _ in range(7)]
    blob = pack_schedule(days)
    back = unpack_schedule(blob)
    for d in back:
        assert d["slots"] == all_slots


# ------------------------------------------------------------------ decode_schedule (full frame)


def test_decode_schedule_parses_live_frame() -> None:
    result = decode_schedule(LIVE_FRAME)
    assert result.get("enabled") is True
    assert "days" in result
    assert len(result["days"]) == 7


def test_decode_schedule_days_match_live_slots() -> None:
    result = decode_schedule(LIVE_FRAME)
    for d in result["days"]:
        assert d["slots"] == LIVE_ACTIVE_SLOTS


def test_decode_schedule_without_enabled_field() -> None:
    # Only field 2, no field 1
    frame = bytes([0x12, 0x38]) + LIVE_BLOB
    result = decode_schedule(frame)
    assert "enabled" not in result
    assert len(result["days"]) == 7


def test_decode_schedule_empty_payload_returns_empty_dict() -> None:
    assert decode_schedule(b"") == {}


def test_decode_schedule_corrupt_payload_returns_empty_dict() -> None:
    assert decode_schedule(b"\xff\xff\xff") == {}


# ------------------------------------------------------------------ slot index ↔ time conversion


@pytest.mark.parametrize(
    "slot, expected_hhmm",
    [
        (0, (0, 0)),
        (1, (0, 30)),
        (22, (11, 0)),
        (23, (11, 30)),
        (24, (12, 0)),
        (25, (12, 30)),
        (29, (14, 30)),
        (32, (16, 0)),
        (47, (23, 30)),
    ],
)
def test_slot_to_time_conversion(slot: int, expected_hhmm: tuple[int, int]) -> None:
    total_minutes = slot * mc.SCHEDULE_SLOT_MINUTES
    h, m = divmod(total_minutes, 60)
    assert (h, m) == expected_hhmm
