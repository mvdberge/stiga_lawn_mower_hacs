"""Tests for schedule_manager conversion helpers."""

from __future__ import annotations

from custom_components.stiga_mower.schedule_manager import (
    _contiguous_blocks,
    _ha_to_stiga,
    _slot_to_timestr,
    _stiga_to_ha,
    _timestr_to_slot,
)

# ------------------------------------------------------------------ _slot_to_timestr


def test_slot_to_timestr_zero():
    assert _slot_to_timestr(0) == "00:00"


def test_slot_to_timestr_hour():
    assert _slot_to_timestr(22) == "11:00"  # 22 * 30 = 660 min = 11 h


def test_slot_to_timestr_half_hour():
    assert _slot_to_timestr(29) == "14:30"  # 29 * 30 = 870 min = 14 h 30 m


def test_slot_to_timestr_midnight():
    assert _slot_to_timestr(48) == "24:00"


# ------------------------------------------------------------------ _timestr_to_slot


def test_timestr_to_slot_on_the_hour():
    assert _timestr_to_slot("11:00") == 22


def test_timestr_to_slot_half_hour():
    assert _timestr_to_slot("14:30") == 29


def test_timestr_to_slot_with_seconds():
    assert _timestr_to_slot("11:00:00") == 22


def test_timestr_to_slot_midnight():
    assert _timestr_to_slot("24:00") == 48


def test_timestr_to_slot_rounds_down():
    # 11:15 → slot 22 (15 min < 30, truncates to :00)
    assert _timestr_to_slot("11:15") == 22


# ------------------------------------------------------------------ _contiguous_blocks


def test_contiguous_blocks_two_windows():
    assert _contiguous_blocks({22, 23, 24, 25, 29, 30, 31, 32}) == [(22, 25), (29, 32)]


def test_contiguous_blocks_empty():
    assert _contiguous_blocks(set()) == []


def test_contiguous_blocks_single_slot():
    assert _contiguous_blocks({5}) == [(5, 5)]


def test_contiguous_blocks_adjacent_single_slots():
    assert _contiguous_blocks({3, 5}) == [(3, 3), (5, 5)]


# ------------------------------------------------------------------ _stiga_to_ha


def _days(slots_per_day: list[set[int]]) -> list[dict]:
    return [{"slots": s} for s in slots_per_day]


def test_stiga_to_ha_single_window():
    days = _days([{22, 23, 24, 25}] + [set()] * 6)  # Mon 11:00–13:00
    result = _stiga_to_ha(days)
    assert result["monday"] == [{"from": "11:00", "to": "13:00"}]
    assert result["tuesday"] == []


def test_stiga_to_ha_two_windows_same_day():
    days = _days([{22, 23, 24, 25, 29, 30, 31, 32}] + [set()] * 6)
    result = _stiga_to_ha(days)
    assert result["monday"] == [
        {"from": "11:00", "to": "13:00"},
        {"from": "14:30", "to": "16:30"},
    ]


def test_stiga_to_ha_all_empty():
    days = _days([set()] * 7)
    result = _stiga_to_ha(days)
    for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
        assert result[day] == []


def test_stiga_to_ha_end_of_day():
    # Slots 46, 47 = 23:00–24:00
    days = _days([{46, 47}] + [set()] * 6)
    result = _stiga_to_ha(days)
    assert result["monday"] == [{"from": "23:00", "to": "24:00"}]


def test_stiga_to_ha_short_days_list():
    # If MQTT sends fewer than 7 days, missing days should be empty.
    # Slots 22+23 form one block 11:00–12:00 (slot 24 exclusive end).
    days = [{"slots": {22, 23}}]  # only Monday
    result = _stiga_to_ha(days)
    assert result["monday"] == [{"from": "11:00", "to": "12:00"}]
    assert result["tuesday"] == []


# ------------------------------------------------------------------ _ha_to_stiga


def test_ha_to_stiga_single_window():
    item = {"monday": [{"from": "11:00", "to": "13:00"}]}
    days = _ha_to_stiga(item)
    assert days[0]["slots"] == {22, 23, 24, 25}
    assert days[1]["slots"] == set()


def test_ha_to_stiga_two_windows():
    item = {"monday": [{"from": "11:00", "to": "13:00"}, {"from": "14:30", "to": "16:30"}]}
    days = _ha_to_stiga(item)
    assert days[0]["slots"] == {22, 23, 24, 25, 29, 30, 31, 32}


def test_ha_to_stiga_all_empty():
    item = {}
    days = _ha_to_stiga(item)
    assert len(days) == 7
    for d in days:
        assert d["slots"] == set()


def test_ha_to_stiga_roundtrip():
    original_days = _days([{22, 23, 24, 25, 29, 30}] + [set()] * 6)
    ha = _stiga_to_ha(original_days)
    recovered = _ha_to_stiga(ha)
    assert recovered[0]["slots"] == {22, 23, 24, 25, 29, 30}
    for i in range(1, 7):
        assert recovered[i]["slots"] == set()
