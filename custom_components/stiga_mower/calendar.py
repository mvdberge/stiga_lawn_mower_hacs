"""Calendar entity exposing the STIGA mowing schedule.

Replaces the prior approach, which created a HA ``schedule.*`` helper entity
via the schedule collection to expose the mowing schedule.

This module instead provides a first-class ``calendar.*`` entity per mower,
which naturally lives under the mower device, supports the HA calendar
editor UI and round-trips edits back to the firmware via
``cmd_schedule_set_enabled`` while preserving the atomic ``enabled``/``blob``
bundling required by the firmware.

Wire model
----------
The mower stores its weekly schedule as a 48-slot bitmap per day (30-min
slots, 0 = 00:00, 47 = 23:30). Each contiguous run of active slots in a day
maps to one weekly-recurring CalendarEvent.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import StigaConfigEntry
from .const import DOMAIN, split_firmware_version
from .coordinator import StigaDataUpdateCoordinator
from .mqtt_constants import SCHEDULE_SLOT_MINUTES, SCHEDULE_SLOTS_PER_DAY
from .mqtt_messages import pack_schedule

_LOGGER = logging.getLogger(__name__)

# Serialise writes: every edit is a read-modify-write of the single atomic
# 48-slot schedule blob, so concurrent edits would race and lose windows.
PARALLEL_UPDATES = 1

# Slot geometry has a single source of truth in mqtt_constants; alias the wire
# constants to the short names this module (and tests) import. Assert the values
# so any future divergence in the wire geometry fails fast instead of silently
# changing calendar behaviour.
SLOTS_PER_DAY = SCHEDULE_SLOTS_PER_DAY
_SLOT_MINUTES = SCHEDULE_SLOT_MINUTES
assert SLOTS_PER_DAY == 48
assert _SLOT_MINUTES == 30
_BYDAY_NAMES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


# ---------------------------------------------------------------- slot helpers


def slot_to_time(slot: int) -> dt.time:
    """30-min slot index → time-of-day. Slot 48 → midnight of next day."""
    minutes = slot * _SLOT_MINUTES
    h, m = divmod(minutes, 60)
    if h >= 24:
        return dt.time(0, 0)
    return dt.time(h, m)


def time_to_slot(t: dt.time) -> int:
    """Time-of-day → 30-min slot index (rounded down). Maxes at 48.

    Correct for a window *start*: an event starting at 10:45 should begin
    mowing from the 10:30 slot.
    """
    return min(t.hour * 2 + t.minute // _SLOT_MINUTES, SLOTS_PER_DAY)


def time_to_end_slot(t: dt.time) -> int:
    """Time-of-day → exclusive end slot index (rounded UP). Maxes at 48.

    Correct for a window *end*: an event ending at 10:45 must cover the
    10:30–11:00 slot, so the exclusive end is the 11:00 boundary (slot 22).
    Rounding down here would silently shorten every window whose end is not
    on the 30-min grid. ``00:00`` is treated as end-of-day (slot 48).
    """
    minutes = t.hour * 60 + t.minute
    if minutes == 0:
        return SLOTS_PER_DAY
    return min(-(-minutes // _SLOT_MINUTES), SLOTS_PER_DAY)


def contiguous_blocks(slots: set[int]) -> list[tuple[int, int]]:
    """Return list of (start_slot, end_slot_inclusive) blocks in ``slots``."""
    if not slots:
        return []
    seq = sorted(slots)
    blocks: list[tuple[int, int]] = []
    start = prev = seq[0]
    for x in seq[1:]:
        if x != prev + 1:
            blocks.append((start, prev))
            start = x
        prev = x
    blocks.append((start, prev))
    return blocks


def make_uid(uuid: str, day_idx: int, start_slot: int) -> str:
    """Deterministic UID for the weekly block starting at (day_idx, start_slot)."""
    return f"stiga-{uuid}-d{day_idx}-s{start_slot}"


def parse_uid(uid: str) -> tuple[int, int] | None:
    """Inverse of :func:`make_uid`. Returns ``(day_idx, start_slot)`` or None."""
    parts = uid.rsplit("-", 2)
    if len(parts) != 3:
        return None
    try:
        if not parts[1].startswith("d") or not parts[2].startswith("s"):
            return None
        return int(parts[1][1:]), int(parts[2][1:])
    except ValueError:
        return None


# ---------------------------------------------------------------- platform setup


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a calendar entity per mower."""
    coordinator: StigaDataUpdateCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new: list[StigaMowingCalendar] = []
        for device in coordinator.data.get("devices", []):
            attrs = device.get("attributes") or {}
            uuid = attrs.get("uuid")
            if not uuid or uuid in known:
                continue
            known.add(uuid)
            new.append(StigaMowingCalendar(coordinator, device))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


# ---------------------------------------------------------------- entity


class StigaMowingCalendar(CoordinatorEntity[StigaDataUpdateCoordinator], CalendarEntity):
    """One calendar per mower, mirroring its weekly mowing schedule."""

    _attr_has_entity_name = True
    _attr_translation_key = "mowing_schedule"
    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
    )

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        attrs = device.get("attributes") or {}
        self._uuid = attrs.get("uuid", "")
        self._mac = attrs.get("mac_address", "")
        self._attr_unique_id = f"stiga_{self._uuid}_mowing_schedule"

    def _device_attrs(self) -> dict[str, Any]:
        for d in self.coordinator.data.get("devices", []):
            if (d.get("attributes") or {}).get("uuid") == self._uuid:
                return d.get("attributes") or {}
        return {}

    @property
    def device_info(self) -> DeviceInfo:
        a = self._device_attrs()
        meta = self.coordinator.data.get("meta", {}).get(self._uuid, {})
        info = DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=a.get("name") or self._uuid,
            manufacturer="STIGA",
            model=meta.get("model_name") or a.get("product_code") or a.get("device_type") or "",
            serial_number=a.get("serial_number") or "",
        )
        hw, fw, _build = split_firmware_version(a.get("firmware_version"))
        if fw:
            info["sw_version"] = fw
        if hw and hw != fw:
            info["hw_version"] = hw
        if mac := a.get("mac_address"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        return info

    def _days(self) -> list[dict[str, set[int]]]:
        return self.coordinator.data.get("live_schedule", {}).get(self._mac, {}).get("days") or []

    # -------------------------- read

    @property
    def event(self) -> CalendarEvent | None:
        """Currently-active or next-upcoming mowing window."""
        now = dt_util.now()
        end = now + dt.timedelta(days=8)
        events = self._materialize_events(now - dt.timedelta(hours=1), end)
        events.sort(key=lambda e: e.start)
        for e in events:
            if e.end_datetime_local > now:
                return e
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: dt.datetime,
        end_date: dt.datetime,
    ) -> list[CalendarEvent]:
        return self._materialize_events(start_date, end_date)

    def _materialize_events(
        self, start_date: dt.datetime, end_date: dt.datetime
    ) -> list[CalendarEvent]:
        """Expand the weekly schedule into concrete events in the given range."""
        start_local = dt_util.as_local(start_date)
        end_local = dt_util.as_local(end_date)
        tz = start_local.tzinfo
        out: list[CalendarEvent] = []

        first_day = (start_local - dt.timedelta(days=1)).date()
        last_day = (end_local + dt.timedelta(days=1)).date()
        n_days = (last_day - first_day).days + 1

        days = self._days()
        if not days:
            return out

        for offset in range(n_days):
            date = first_day + dt.timedelta(days=offset)
            day_idx = date.weekday()  # 0=Mon ... 6=Sun, matches STIGA day order
            if day_idx >= len(days):
                continue
            slots = days[day_idx].get("slots") or set()
            for start_slot, end_slot in contiguous_blocks(set(slots)):
                start_t = slot_to_time(start_slot)
                # end_slot is INCLUSIVE; exclusive end = end_slot + 1
                exclusive_end = end_slot + 1
                start_dt = dt.datetime.combine(date, start_t, tzinfo=tz)
                if exclusive_end >= SLOTS_PER_DAY:
                    end_dt = dt.datetime.combine(
                        date + dt.timedelta(days=1), dt.time(0, 0), tzinfo=tz
                    )
                else:
                    end_dt = dt.datetime.combine(date, slot_to_time(exclusive_end), tzinfo=tz)
                if end_dt <= start_local or start_dt >= end_local:
                    continue
                out.append(
                    CalendarEvent(
                        start=start_dt,
                        end=end_dt,
                        summary="Mowing",
                        uid=make_uid(self._uuid, day_idx, start_slot),
                        rrule=f"FREQ=WEEKLY;BYDAY={_BYDAY_NAMES[day_idx]}",
                    )
                )
        return out

    # -------------------------- write

    async def async_create_event(self, **kwargs: Any) -> None:
        """Add one or more weekly recurring mowing windows.

        Accepts the standard HA calendar payload (``dtstart``, ``dtend``,
        optional ``rrule``). The window is snapped to the firmware's 30-min
        slot grid. Multiple ``BYDAY=`` values in the rrule add the block to
        every selected weekday.
        """
        start = kwargs.get("dtstart")
        end = kwargs.get("dtend")
        if not isinstance(start, dt.datetime) or not isinstance(end, dt.datetime):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="calendar_no_all_day"
            )
        rrule = kwargs.get("rrule")
        target_day_indices = _byday_indices_from_rrule(rrule, default_weekday=start.weekday())
        await self._modify(add=[(d, start.time(), end.time()) for d in target_day_indices])

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        parsed = parse_uid(uid)
        if parsed is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="calendar_unknown_uid",
                translation_placeholders={"uid": str(uid)},
            )
        await self._modify(remove=[parsed])

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Move a window in a single atomic write.

        The window is deleted and re-created in one read-modify-write of the
        48-slot blob, then published exactly once. Chaining separate delete +
        create would send two SCHEDULING_SETTINGS_UPDATE frames, and a failure
        of the second would drop the window entirely.
        """
        parsed = parse_uid(uid)
        if parsed is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="calendar_unknown_uid",
                translation_placeholders={"uid": str(uid)},
            )
        start = event.get("dtstart")
        end = event.get("dtend")
        if not isinstance(start, dt.datetime) or not isinstance(end, dt.datetime):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="calendar_no_all_day"
            )
        target_day_indices = _byday_indices_from_rrule(
            event.get("rrule"), default_weekday=start.weekday()
        )
        await self._modify(
            remove=[parsed],
            add=[(d, start.time(), end.time()) for d in target_day_indices],
        )

    # -------------------------- write helpers

    def _ensure_loaded(self) -> None:
        """Refuse edits until the mower has reported its schedule.

        Before the first SCHEDULING frame, ``live_schedule[mac]`` has no
        ``days`` key. Writing then would build the atomic blob from an empty
        7-day list and wipe the user's real weekly plan (which we simply have
        not received yet), so refuse the edit instead.
        """
        sched = self.coordinator.data.get("live_schedule", {}).get(self._mac, {})
        if "days" not in sched:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="calendar_schedule_not_loaded"
            )

    async def _modify(
        self,
        *,
        add: list[tuple[int, dt.time, dt.time]] | None = None,
        remove: list[tuple[int, int]] | None = None,
    ) -> None:
        """Apply removals and additions to the weekly grid, then publish once."""
        self._ensure_loaded()
        days = [dict(d) for d in self._days()]
        while len(days) < 7:
            days.append({"slots": set()})
        before = [frozenset(d.get("slots") or ()) for d in days]

        for day_idx, start_slot in remove or []:
            if not 0 <= day_idx < len(days):
                continue
            slots = set(days[day_idx].get("slots") or set())
            block = _find_block(slots, start_slot)
            if block is not None:
                for s in range(block[0], block[1] + 1):
                    slots.discard(s)
            days[day_idx]["slots"] = slots

        for day_idx, start_t, end_t in add or []:
            if not 0 <= day_idx < len(days):
                continue
            start_slot = time_to_slot(start_t)
            end_slot = max(time_to_end_slot(end_t), start_slot + 1)
            slots = set(days[day_idx].get("slots") or set())
            for s in range(start_slot, end_slot):
                if 0 <= s < SLOTS_PER_DAY:
                    slots.add(s)
            days[day_idx]["slots"] = slots

        if [frozenset(d.get("slots") or ()) for d in days] == before:
            _LOGGER.debug("schedule edit is a no-op; skipping MQTT write")
            return
        await self._publish(days)

    async def _publish(self, days: list[dict[str, Any]]) -> None:
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="mqtt_not_connected"
            )
        blob = pack_schedule(days)
        # SCHEDULING_SETTINGS_UPDATE is atomic: enabled + blob must always travel
        # together, otherwise the firmware resets `enabled` to its proto3 default.
        sched = self.coordinator.data.get("live_schedule", {}).get(self._mac, {})
        enabled = bool(sched.get("enabled"))
        await mqtt.cmd_schedule_set_enabled(self._mac, enabled, blob=blob)
        # Optimistic update so the calendar UI reflects the change immediately.
        self.coordinator.apply_live_schedule(self._mac, {"days": days})


# ---------------------------------------------------------------- rrule helpers


def _byday_indices_from_rrule(rrule: str | None, *, default_weekday: int) -> list[int]:
    """Parse ``FREQ=WEEKLY;BYDAY=MO,WE`` style strings into [0, 2].

    Falls back to ``[default_weekday]`` when no BYDAY is present so a non-
    recurring event still lands on a sensible day.
    """
    if not rrule:
        return [default_weekday]
    byday_value: str | None = None
    for part in rrule.split(";"):
        key, _, value = part.partition("=")
        if key.strip().upper() == "BYDAY":
            byday_value = value
            break
    if not byday_value:
        return [default_weekday]
    indices: list[int] = []
    for token in byday_value.split(","):
        code = token.strip().upper()
        # rrule allows BYDAY=+1MO etc. — strip leading numeric prefix.
        code = code.lstrip("+-0123456789")
        if code in _BYDAY_NAMES:
            indices.append(_BYDAY_NAMES.index(code))
    return indices or [default_weekday]


def _find_block(slots: set[int], start_slot: int) -> tuple[int, int] | None:
    """Return (start, end_inclusive) for the contiguous block beginning at
    ``start_slot``, or None if no such block exists."""
    if start_slot not in slots:
        return None
    end = start_slot
    while end + 1 in slots:
        end += 1
    # Also reject if start_slot is not the actual block start.
    if start_slot - 1 in slots:
        return None
    return (start_slot, end)
