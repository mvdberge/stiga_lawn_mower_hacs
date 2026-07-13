"""Tests for the calendar.py module (mowing-schedule calendar entity)."""

from __future__ import annotations

import datetime as dt

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.stiga_mower.calendar import (
    SLOTS_PER_DAY,
    StigaMowingCalendar,
    _byday_indices_from_rrule,
    _find_block,
    contiguous_blocks,
    make_uid,
    parse_uid,
    slot_to_time,
    time_to_end_slot,
    time_to_slot,
)

from ._entity_helpers import make_coordinator

# ---------------------------------------------------------------- pure helpers


def test_slot_to_time_zero() -> None:
    assert slot_to_time(0) == dt.time(0, 0)


def test_slot_to_time_hour_boundary() -> None:
    assert slot_to_time(22) == dt.time(11, 0)


def test_slot_to_time_half_hour() -> None:
    assert slot_to_time(29) == dt.time(14, 30)


def test_slot_to_time_eod_wraps_to_midnight() -> None:
    # Slot 48 = 24:00; we represent it as midnight of next day, but the
    # function itself returns time(0, 0).
    assert slot_to_time(SLOTS_PER_DAY) == dt.time(0, 0)


def test_time_to_slot_round_down() -> None:
    assert time_to_slot(dt.time(11, 29)) == 22  # 11:29 → 11:00 bucket


def test_time_to_slot_exact_boundary() -> None:
    assert time_to_slot(dt.time(14, 30)) == 29


def test_time_to_end_slot_rounds_up() -> None:
    # 10:45 must round UP to the 11:00 boundary (slot 22) so the window is not
    # silently truncated.
    assert time_to_end_slot(dt.time(10, 45)) == 22


def test_time_to_end_slot_exact_boundary_unchanged() -> None:
    assert time_to_end_slot(dt.time(11, 0)) == 22


def test_time_to_end_slot_midnight_is_end_of_day() -> None:
    assert time_to_end_slot(dt.time(0, 0)) == SLOTS_PER_DAY


def test_contiguous_blocks_two_windows() -> None:
    blocks = contiguous_blocks({22, 23, 24, 29, 30, 31})
    assert blocks == [(22, 24), (29, 31)]


def test_contiguous_blocks_empty() -> None:
    assert contiguous_blocks(set()) == []


def test_make_and_parse_uid_roundtrip() -> None:
    uid = make_uid("u1", 3, 22)
    assert parse_uid(uid) == (3, 22)


def test_parse_uid_returns_none_for_garbage() -> None:
    assert parse_uid("not-a-stiga-uid") is None
    assert parse_uid("stiga-u1-d3") is None


def test_byday_indices_from_rrule_single_day() -> None:
    assert _byday_indices_from_rrule("FREQ=WEEKLY;BYDAY=MO", default_weekday=5) == [0]


def test_byday_indices_from_rrule_multiple_days() -> None:
    result = _byday_indices_from_rrule("FREQ=WEEKLY;BYDAY=MO,WE,FR", default_weekday=0)
    assert result == [0, 2, 4]


def test_byday_indices_fallback_to_default_when_no_rrule() -> None:
    assert _byday_indices_from_rrule(None, default_weekday=3) == [3]


def test_byday_indices_fallback_to_default_when_no_byday() -> None:
    assert _byday_indices_from_rrule("FREQ=DAILY", default_weekday=2) == [2]


def test_find_block_matches_block_start() -> None:
    assert _find_block({10, 11, 12, 20, 21}, 10) == (10, 12)


def test_find_block_rejects_mid_block_start() -> None:
    # start_slot 11 is in the middle of {10, 11, 12} — not a block start.
    assert _find_block({10, 11, 12}, 11) is None


def test_find_block_returns_none_for_missing_slot() -> None:
    assert _find_block({10, 11, 12}, 5) is None


# ---------------------------------------------------------------- entity


def _calendar(coordinator) -> StigaMowingCalendar:
    device = coordinator.data["devices"][0]
    return StigaMowingCalendar(coordinator, device)


def _set_schedule(coordinator, days: list[set[int]], *, enabled: bool = True) -> None:
    coordinator._live_schedule["MAC1"] = {
        "enabled": enabled,
        "days": [{"slots": s} for s in days],
    }
    coordinator.async_set_updated_data(
        coordinator._build_data(rest_statuses={"u1": {"has_data": True}})
    )


def test_calendar_no_schedule_yields_no_events(hass) -> None:
    c = make_coordinator(hass)
    cal = _calendar(c)
    now = dt_util.now()
    events = cal._materialize_events(now, now + dt.timedelta(days=7))
    assert events == []


@pytest.mark.asyncio
async def test_calendar_get_events_returns_one_event_per_block_per_week(hass, freezer) -> None:
    """Monday 11:00–13:00 + Wednesday 14:30–16:30 expand to two weekly events."""
    freezer.move_to("2026-05-18 09:00:00+02:00")  # a Monday in CEST
    c = make_coordinator(hass)
    # Monday=0: 22-25 (11:00–13:00); Wed=2: 29-32 (14:30–16:30)
    _set_schedule(
        c,
        [
            {22, 23, 24, 25},
            set(),
            {29, 30, 31, 32},
            set(),
            set(),
            set(),
            set(),
        ],
    )
    cal = _calendar(c)
    start = dt_util.now()
    end = start + dt.timedelta(days=7)
    events = await cal.async_get_events(hass, start, end)
    summaries = [(e.start.weekday(), e.start.time(), e.end.time()) for e in events]
    assert (0, dt.time(11, 0), dt.time(13, 0)) in summaries
    assert (2, dt.time(14, 30), dt.time(16, 30)) in summaries
    assert all(e.rrule and "FREQ=WEEKLY" in e.rrule for e in events)


@pytest.mark.asyncio
async def test_calendar_event_property_returns_next_upcoming(hass, freezer) -> None:
    freezer.move_to("2026-05-18 10:30:00+02:00")  # Monday before the window
    c = make_coordinator(hass)
    _set_schedule(c, [{22, 23, 24, 25}] + [set()] * 6)
    cal = _calendar(c)
    nxt = cal.event
    assert nxt is not None
    assert nxt.start.time() == dt.time(11, 0)


@pytest.mark.asyncio
async def test_calendar_event_property_returns_current_event_during_window(hass, freezer) -> None:
    freezer.move_to("2026-05-18 11:30:00+02:00")  # Monday inside the window
    c = make_coordinator(hass)
    _set_schedule(c, [{22, 23, 24, 25}] + [set()] * 6)
    cal = _calendar(c)
    nxt = cal.event
    assert nxt is not None
    assert nxt.start.time() == dt.time(11, 0)
    assert nxt.end.time() == dt.time(13, 0)


@pytest.mark.asyncio
async def test_calendar_create_event_adds_slots_and_publishes(hass) -> None:
    c = make_coordinator(hass)
    _set_schedule(c, [set()] * 7)
    cal = _calendar(c)
    await cal.async_create_event(
        dtstart=dt.datetime(2026, 5, 18, 9, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),  # Mon
        dtend=dt.datetime(2026, 5, 18, 10, 30, tzinfo=dt_util.DEFAULT_TIME_ZONE),
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )
    c.mqtt.cmd_schedule_set_enabled.assert_awaited_once()
    args, kwargs = c.mqtt.cmd_schedule_set_enabled.call_args
    assert args[0] == "MAC1"
    assert args[1] is True  # bundled current enabled state
    assert "blob" in kwargs
    new_days = c.data["live_schedule"]["MAC1"]["days"]
    # 9:00=slot 18, 10:30=slot 21 (end exclusive). So slots 18,19,20 active.
    assert new_days[0]["slots"] == {18, 19, 20}
    for i in range(1, 7):
        assert new_days[i]["slots"] == set()


@pytest.mark.asyncio
async def test_calendar_create_event_multi_byday_targets_all_listed_days(hass) -> None:
    c = make_coordinator(hass)
    _set_schedule(c, [set()] * 7)
    cal = _calendar(c)
    await cal.async_create_event(
        dtstart=dt.datetime(2026, 5, 18, 8, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),
        dtend=dt.datetime(2026, 5, 18, 9, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),
        rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR",
    )
    new_days = c.data["live_schedule"]["MAC1"]["days"]
    expected_slots = {16, 17}  # 08:00=16, 09:00=18 (exclusive) → 16, 17
    for day_idx in (0, 2, 4):
        assert new_days[day_idx]["slots"] == expected_slots
    for day_idx in (1, 3, 5, 6):
        assert new_days[day_idx]["slots"] == set()


@pytest.mark.asyncio
async def test_calendar_delete_event_removes_full_block(hass) -> None:
    c = make_coordinator(hass)
    _set_schedule(c, [{16, 17, 18, 19}] + [set()] * 6)
    cal = _calendar(c)
    await cal.async_delete_event(uid=make_uid(cal._uuid, 0, 16))
    new_days = c.data["live_schedule"]["MAC1"]["days"]
    assert new_days[0]["slots"] == set()


@pytest.mark.asyncio
async def test_calendar_delete_event_unknown_uid_raises(hass) -> None:
    c = make_coordinator(hass)
    _set_schedule(c, [set()] * 7)
    cal = _calendar(c)
    with pytest.raises(HomeAssistantError):
        await cal.async_delete_event(uid="not-a-stiga-uid")


@pytest.mark.asyncio
async def test_calendar_write_requires_mqtt_connected(hass) -> None:
    c = make_coordinator(hass, mqtt_connected=False)
    _set_schedule(c, [set()] * 7)
    cal = _calendar(c)
    with pytest.raises(HomeAssistantError) as err:
        await cal.async_create_event(
            dtstart=dt.datetime(2026, 5, 18, 9, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),
            dtend=dt.datetime(2026, 5, 18, 10, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),
            rrule="FREQ=WEEKLY;BYDAY=MO",
        )
    assert err.value.translation_key == "mqtt_not_connected"


@pytest.mark.asyncio
async def test_calendar_create_event_bundles_current_enabled_state(hass) -> None:
    """Edits MUST carry the live `enabled` flag — the firmware is atomic on
    SCHEDULING_SETTINGS_UPDATE (cmd 20) and would reset enabled to False
    otherwise."""
    c = make_coordinator(hass)
    _set_schedule(c, [set()] * 7, enabled=False)
    cal = _calendar(c)
    await cal.async_create_event(
        dtstart=dt.datetime(2026, 5, 18, 9, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),
        dtend=dt.datetime(2026, 5, 18, 10, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )
    args, _ = c.mqtt.cmd_schedule_set_enabled.call_args
    assert args[1] is False


@pytest.mark.asyncio
async def test_calendar_create_event_rounds_end_up(hass) -> None:
    """An end time off the 30-min grid must round UP, not truncate the window
    (regression for the end-slot rounding bug)."""
    c = make_coordinator(hass)
    _set_schedule(c, [set()] * 7)
    cal = _calendar(c)
    await cal.async_create_event(
        dtstart=dt.datetime(2026, 5, 18, 9, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),  # Mon
        dtend=dt.datetime(2026, 5, 18, 10, 45, tzinfo=dt_util.DEFAULT_TIME_ZONE),
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )
    new_days = c.data["live_schedule"]["MAC1"]["days"]
    # 09:00=slot 18; 10:45 rounds up to the 11:00 boundary (slot 22, exclusive)
    # → slots 18,19,20,21 active (09:00–11:00).  Before the fix this dropped 21.
    assert new_days[0]["slots"] == {18, 19, 20, 21}


@pytest.mark.asyncio
async def test_calendar_create_event_until_midnight_fills_to_eod(hass) -> None:
    """A window ending at next-day midnight must fill through the last slot."""
    c = make_coordinator(hass)
    _set_schedule(c, [set()] * 7)
    cal = _calendar(c)
    await cal.async_create_event(
        dtstart=dt.datetime(2026, 5, 18, 22, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),  # Mon
        dtend=dt.datetime(2026, 5, 19, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE),  # Tue 00:00
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )
    new_days = c.data["live_schedule"]["MAC1"]["days"]
    # 22:00=slot 44 … 23:30=slot 47 (last slot of the day).
    assert new_days[0]["slots"] == {44, 45, 46, 47}


@pytest.mark.asyncio
async def test_calendar_materialize_eod_block_ends_next_midnight(hass, freezer) -> None:
    freezer.move_to("2026-05-18 08:00:00+02:00")  # Monday
    c = make_coordinator(hass)
    _set_schedule(c, [{44, 45, 46, 47}] + [set()] * 6)  # Mon 22:00–24:00
    cal = _calendar(c)
    start = dt_util.now()
    events = await cal.async_get_events(hass, start, start + dt.timedelta(days=2))
    mon = next(e for e in events if e.start.weekday() == 0)
    assert mon.start.time() == dt.time(22, 0)
    assert mon.end.time() == dt.time(0, 0)
    assert mon.end.date() == mon.start.date() + dt.timedelta(days=1)


def test_calendar_empty_days_event_property_is_none(hass) -> None:
    c = make_coordinator(hass)
    _set_schedule(c, [set()] * 7)
    cal = _calendar(c)
    assert cal.event is None


@pytest.mark.asyncio
async def test_calendar_materialize_preserves_wall_time_across_dst(hass, freezer) -> None:
    """Spring-forward must not shift the window's wall-clock time — a naive/UTC
    datetime bug would drift it by an hour."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-03-27 08:00:00+01:00")  # Friday before the DST change
    c = make_coordinator(hass)
    # Sunday=6: 11:00–13:00; 2026-03-29 is the CET→CEST spring-forward day.
    _set_schedule(c, [set()] * 6 + [{22, 23, 24, 25}])
    cal = _calendar(c)
    start = dt_util.now()
    events = await cal.async_get_events(hass, start, start + dt.timedelta(days=4))
    sun = next(e for e in events if e.start.weekday() == 6)
    assert sun.start.time() == dt.time(11, 0)
    assert sun.end.time() == dt.time(13, 0)
    # The window is after the 02:00→03:00 jump, so CEST (UTC+2) is in effect.
    assert sun.start.utcoffset() == dt.timedelta(hours=2)
