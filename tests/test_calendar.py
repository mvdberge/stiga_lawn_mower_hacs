"""Tests for StigaCalendar — mowing schedule read/write."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.stiga_mower.calendar import (
    StigaCalendar,
    _contiguous_blocks,
    _dt_to_slot,
    _find_block,
)
from custom_components.stiga_mower.coordinator import StigaDataUpdateCoordinator

# ------------------------------------------------------------------ fixtures

LIVE_ACTIVE_SLOTS = {22, 23, 24, 25, 29, 30, 31, 32}
# 11:00–13:00 and 14:30–16:30, same every day


def _make_coordinator(hass, *, live_schedule=None, mqtt_connected=True):
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)
    c._devices = [{"attributes": {"uuid": "u1", "name": "Bot", "mac_address": "MAC1"}}]
    if live_schedule is not None:
        c._live_schedule["MAC1"] = live_schedule
    c.async_set_updated_data(c._build_data(rest_statuses={"u1": {}}))

    mqtt = MagicMock()
    mqtt.connected = mqtt_connected
    mqtt.cmd_schedule_update = AsyncMock()
    c.mqtt = mqtt
    return c


def _calendar(coordinator):
    device = coordinator.data["devices"][0]
    return StigaCalendar(coordinator, device)


def _days_all_active(slots=LIVE_ACTIVE_SLOTS):
    return {"enabled": True, "days": [{"slots": set(slots)} for _ in range(7)]}


# ------------------------------------------------------------------ helper unit tests


def test_dt_to_slot_on_the_hour():
    dt = datetime(2024, 1, 1, 11, 0)
    assert _dt_to_slot(dt) == 22  # 11*2 = 22


def test_dt_to_slot_half_hour():
    dt = datetime(2024, 1, 1, 14, 30)
    assert _dt_to_slot(dt) == 29  # 14*2+1 = 29


def test_dt_to_slot_rounds_down():
    dt = datetime(2024, 1, 1, 11, 15)
    assert _dt_to_slot(dt) == 22  # 11*2 = 22 (15 min rounds down)


def test_contiguous_blocks_two_windows():
    slots = {22, 23, 24, 25, 29, 30, 31, 32}
    blocks = _contiguous_blocks(slots)
    assert blocks == [(22, 25), (29, 32)]


def test_contiguous_blocks_empty():
    assert _contiguous_blocks(set()) == []


def test_contiguous_blocks_single_slot():
    assert _contiguous_blocks({5}) == [(5, 5)]


def test_find_block_finds_correct_block():
    slots = {22, 23, 24, 25, 29, 30}
    block = _find_block(slots, 22)
    assert block == {22, 23, 24, 25}


def test_find_block_second_window():
    slots = {22, 23, 24, 25, 29, 30}
    block = _find_block(slots, 29)
    assert block == {29, 30}


def test_find_block_missing_start_returns_none():
    assert _find_block({22, 23}, 10) is None


# ------------------------------------------------------------------ availability


def test_calendar_unavailable_when_no_live_schedule(hass):
    c = _make_coordinator(hass)
    cal = _calendar(c)
    assert cal.available is False


def test_calendar_available_when_live_schedule_present(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    assert cal.available is True


# ------------------------------------------------------------------ event property


def test_event_returns_next_upcoming_window(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    event = cal.event
    assert event is not None
    assert "Mowing" in event.summary


def test_event_returns_none_when_no_schedule(hass):
    c = _make_coordinator(hass)
    cal = _calendar(c)
    assert cal.event is None


def test_event_returns_none_when_all_slots_empty(hass):
    c = _make_coordinator(
        hass, live_schedule={"enabled": True, "days": [{"slots": set()} for _ in range(7)]}
    )
    cal = _calendar(c)
    assert cal.event is None


# ------------------------------------------------------------------ async_get_events


@pytest.mark.asyncio
async def test_get_events_returns_windows_in_range(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    start = datetime(2024, 4, 29, 0, 0, tzinfo=UTC)  # Monday
    end = start + timedelta(days=1)
    events = await cal.async_get_events(hass, start, end)
    assert len(events) == 2
    times = [(e.start.hour, e.start.minute, e.end.hour, e.end.minute) for e in events]
    assert (11, 0, 13, 0) in times
    assert (14, 30, 16, 30) in times


@pytest.mark.asyncio
async def test_get_events_empty_when_no_schedule(hass):
    c = _make_coordinator(hass)
    cal = _calendar(c)
    start = datetime(2024, 4, 29, 0, 0, tzinfo=UTC)
    events = await cal.async_get_events(hass, start, start + timedelta(days=7))
    assert events == []


@pytest.mark.asyncio
async def test_get_events_covers_full_week(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    start = datetime(2024, 4, 29, 0, 0, tzinfo=UTC)  # Monday
    end = start + timedelta(days=7)
    events = await cal.async_get_events(hass, start, end)
    # 2 windows × 7 days = 14 events
    assert len(events) == 14


@pytest.mark.asyncio
async def test_get_events_uid_encodes_day_and_slot(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    start = datetime(2024, 4, 29, 0, 0, tzinfo=UTC)  # Monday (weekday=0)
    end = start + timedelta(days=1)
    events = await cal.async_get_events(hass, start, end)
    uids = {e.uid for e in events}
    assert "0:22" in uids  # day 0, slot 22 = Mon 11:00
    assert "0:29" in uids  # day 0, slot 29 = Mon 14:30


@pytest.mark.asyncio
async def test_get_events_rrule_is_weekly(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    start = datetime(2024, 4, 29, 0, 0, tzinfo=UTC)
    events = await cal.async_get_events(hass, start, start + timedelta(days=1))
    for e in events:
        assert e.rrule is not None
        assert "FREQ=WEEKLY" in e.rrule


# ------------------------------------------------------------------ create_event


@pytest.mark.asyncio
async def test_create_event_calls_cmd_schedule_update(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active({0}))  # only slot 0 active
    cal = _calendar(c)
    # Add a new window: Tuesday 08:00–09:00 (slots 16,17)
    await cal.async_create_event(
        dtstart=datetime(2024, 4, 30, 8, 0),  # Tuesday
        dtend=datetime(2024, 4, 30, 9, 0),
    )
    c.mqtt.cmd_schedule_update.assert_awaited_once()
    mac, blob = c.mqtt.cmd_schedule_update.call_args.args
    assert mac == "MAC1"
    assert isinstance(blob, bytes)
    # Verify the blob decoded contains the new slots
    from custom_components.stiga_mower.mqtt_messages import unpack_schedule

    days = unpack_schedule(blob)
    assert {16, 17}.issubset(days[1]["slots"])  # day 1 = Tuesday


@pytest.mark.asyncio
async def test_create_event_raises_on_overlap(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    with pytest.raises(Exception, match="overlaps"):
        await cal.async_create_event(
            dtstart=datetime(2024, 4, 29, 11, 0),  # Mon 11:00 — already active
            dtend=datetime(2024, 4, 29, 12, 0),
        )


@pytest.mark.asyncio
async def test_create_event_raises_when_mqtt_disconnected(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active(), mqtt_connected=False)
    cal = _calendar(c)
    with pytest.raises(Exception, match="MQTT not connected"):
        await cal.async_create_event(
            dtstart=datetime(2024, 4, 29, 6, 0),
            dtend=datetime(2024, 4, 29, 7, 0),
        )


@pytest.mark.asyncio
async def test_create_event_single_slot(hass):
    c = _make_coordinator(
        hass, live_schedule={"enabled": True, "days": [{"slots": set()} for _ in range(7)]}
    )
    cal = _calendar(c)
    await cal.async_create_event(
        dtstart=datetime(2024, 4, 29, 6, 0),  # slot 12
        dtend=datetime(2024, 4, 29, 6, 30),  # slot 13 exclusive → only slot 12
    )
    c.mqtt.cmd_schedule_update.assert_awaited_once()
    _, blob = c.mqtt.cmd_schedule_update.call_args.args
    from custom_components.stiga_mower.mqtt_messages import unpack_schedule

    days = unpack_schedule(blob)
    assert 12 in days[0]["slots"]
    assert 13 not in days[0]["slots"]


# ------------------------------------------------------------------ delete_event


@pytest.mark.asyncio
async def test_delete_event_removes_block(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    # Delete Monday 11:00 window (uid "0:22")
    await cal.async_delete_event("0:22")
    c.mqtt.cmd_schedule_update.assert_awaited_once()
    _, blob = c.mqtt.cmd_schedule_update.call_args.args
    from custom_components.stiga_mower.mqtt_messages import unpack_schedule

    days = unpack_schedule(blob)
    # Slots 22-25 should be gone, 29-32 remain
    assert not {22, 23, 24, 25} & days[0]["slots"]
    assert {29, 30, 31, 32}.issubset(days[0]["slots"])


@pytest.mark.asyncio
async def test_delete_event_raises_on_invalid_uid(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    with pytest.raises(Exception, match="Invalid event uid"):
        await cal.async_delete_event("not-a-valid-uid")


@pytest.mark.asyncio
async def test_delete_event_raises_when_block_not_found(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active())
    cal = _calendar(c)
    with pytest.raises(Exception, match="No mowing window"):
        await cal.async_delete_event("0:0")  # slot 0 not active


@pytest.mark.asyncio
async def test_delete_event_raises_when_mqtt_disconnected(hass):
    c = _make_coordinator(hass, live_schedule=_days_all_active(), mqtt_connected=False)
    cal = _calendar(c)
    with pytest.raises(Exception, match="MQTT not connected"):
        await cal.async_delete_event("0:22")
