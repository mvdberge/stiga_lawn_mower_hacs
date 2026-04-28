"""STIGA calendar entity — mowing schedule read/write via MQTT."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import StigaConfigEntry
from . import mqtt_constants as mc
from .const import DOMAIN
from .coordinator import StigaDataUpdateCoordinator
from .mqtt_messages import pack_schedule

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

# Weekday names used in event summaries (Mon=0 … Sun=6, matches Python weekday())
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StigaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one calendar entity per STIGA robot."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[StigaCalendar] = []
        for device in coordinator.data.get("devices", []):
            uuid = _dev_uuid(device)
            if not uuid or uuid in known:
                continue
            known.add(uuid)
            new_entities.append(StigaCalendar(coordinator, device))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))
    _add_new_entities()


class StigaCalendar(CoordinatorEntity[StigaDataUpdateCoordinator], CalendarEntity):
    """Weekly mowing schedule for one STIGA robot.

    Each active time-window in the schedule is surfaced as a recurring
    HA CalendarEvent with RRULE FREQ=WEEKLY;BYDAY=<weekday>.  Writing
    (CREATE / DELETE) translates back to a ``SCHEDULING_SETTINGS_UPDATE``
    command via MQTT.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "mowing_schedule"
    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.DELETE_EVENT
    )

    def __init__(
        self,
        coordinator: StigaDataUpdateCoordinator,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        attrs = device.get("attributes") or {}
        self._uuid = attrs.get("uuid", "")
        self._mac = attrs.get("mac_address", "")
        self._attr_unique_id = f"stiga_{self._uuid}_mowing_schedule"

    # ---------------------------------------------------------------- helpers

    def _device_attrs(self) -> dict:
        for d in self.coordinator.data.get("devices", []):
            if _dev_uuid(d) == self._uuid:
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
        if fw := a.get("firmware_version"):
            info["sw_version"] = fw
        if mac := a.get("mac_address"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        return info

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._live_schedule() is not None

    def _live_schedule(self) -> dict | None:
        return self.coordinator.data.get("live_schedule", {}).get(self._mac)

    def _days(self) -> list[dict]:
        sched = self._live_schedule()
        if sched is None:
            return []
        days = sched.get("days")
        if days is None:
            return []
        return days

    # ---------------------------------------------------------------- CalendarEntity API

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming mowing window (required by CalendarEntity)."""
        now = dt_util.now()
        for event in self._generate_events(now, now + timedelta(days=7)):
            if event.end >= now:
                return event
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return all mowing windows that overlap [start_date, end_date]."""
        return list(self._generate_events(start_date, end_date))

    # ---------------------------------------------------------------- write operations

    async def async_create_event(self, **kwargs) -> None:
        """Add a new mowing window to the schedule.

        HA passes ``dtstart`` and ``dtend`` as ``datetime`` objects.
        Both are rounded to the nearest 30-minute slot boundary.
        The weekday is taken from ``dtstart``.
        """
        self._require_mqtt()
        dtstart: datetime = kwargs["dtstart"]
        dtend: datetime = kwargs["dtend"]

        day_index = dtstart.weekday()  # 0=Mon … 6=Sun
        start_slot = _dt_to_slot(dtstart)
        end_slot = _dt_to_slot(dtend) - 1  # end is exclusive

        if start_slot > end_slot:
            raise HomeAssistantError("Event must span at least one 30-minute slot")
        if start_slot < 0 or end_slot >= mc.SCHEDULE_SLOTS_PER_DAY:
            raise HomeAssistantError("Event times must be within 00:00–24:00")

        days = self._mutable_days()
        existing = days[day_index]["slots"]
        new_slots = set(range(start_slot, end_slot + 1))
        if existing & new_slots:
            raise HomeAssistantError("New window overlaps an existing mowing window")

        days[day_index]["slots"] = existing | new_slots
        await self._send_schedule(days)

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Remove the mowing window identified by ``uid``.

        The uid encodes ``<day_index>:<start_slot>`` so we can locate the
        exact block to erase without ambiguity.
        """
        self._require_mqtt()
        try:
            day_index, start_slot = (int(x) for x in uid.split(":"))
        except (ValueError, AttributeError) as err:
            raise HomeAssistantError(f"Invalid event uid {uid!r}") from err

        days = self._mutable_days()
        block_slots = _find_block(days[day_index]["slots"], start_slot)
        if block_slots is None:
            raise HomeAssistantError(
                f"No mowing window starting at slot {start_slot} on day {day_index}"
            )

        days[day_index]["slots"] -= block_slots
        await self._send_schedule(days)

    # ---------------------------------------------------------------- internals

    def _require_mqtt(self) -> None:
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.connected or not self._mac:
            raise HomeAssistantError("Cannot update schedule: MQTT not connected")

    def _mutable_days(self) -> list[dict]:
        """Return a deep-copyable list of day-dicts for mutation."""
        return [{"slots": set(d.get("slots", set()))} for d in self._days()]

    async def _send_schedule(self, days: list[dict]) -> None:
        blob = pack_schedule(days)
        await self.coordinator.mqtt.cmd_schedule_update(self._mac, blob)

    def _generate_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Yield CalendarEvents for all active windows in [start, end)."""
        events: list[CalendarEvent] = []
        days = self._days()
        if not days:
            return events

        # HA requires timezone-aware datetimes in CalendarEvent.
        tz = dt_util.get_default_time_zone()
        current = start.replace(hour=0, minute=0, second=0, microsecond=0)
        if current.tzinfo is None:
            current = current.replace(tzinfo=tz)

        while current < end:
            day_index = current.weekday()
            if day_index < len(days):
                slots = days[day_index].get("slots", set())
                for block_start, block_end in _contiguous_blocks(slots):
                    ev_start = current + timedelta(minutes=block_start * mc.SCHEDULE_SLOT_MINUTES)
                    ev_end = current + timedelta(minutes=(block_end + 1) * mc.SCHEDULE_SLOT_MINUTES)
                    if ev_end <= start or ev_start >= end:
                        current += timedelta(days=1)
                        continue
                    uid = f"{day_index}:{block_start}"
                    events.append(
                        CalendarEvent(
                            start=ev_start,
                            end=ev_end,
                            summary=f"Mowing ({_WEEKDAY_NAMES[day_index]})",
                            uid=uid,
                            rrule=f"FREQ=WEEKLY;BYDAY={_BYDAY[day_index]}",
                        )
                    )
            current += timedelta(days=1)
        return events


# ---------------------------------------------------------------- module-level helpers

_BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _dt_to_slot(dt: datetime) -> int:
    """Convert a datetime to a 30-minute slot index (rounds down)."""
    return dt.hour * 2 + dt.minute // 30


def _contiguous_blocks(slots: set[int]) -> list[tuple[int, int]]:
    """Return sorted list of (start_slot, end_slot) for each contiguous run."""
    if not slots:
        return []
    sorted_slots = sorted(slots)
    blocks: list[tuple[int, int]] = []
    block_start = sorted_slots[0]
    prev = sorted_slots[0]
    for s in sorted_slots[1:]:
        if s != prev + 1:
            blocks.append((block_start, prev))
            block_start = s
        prev = s
    blocks.append((block_start, prev))
    return blocks


def _find_block(slots: set[int], start_slot: int) -> set[int] | None:
    """Return the set of slots in the contiguous block starting at start_slot."""
    if start_slot not in slots:
        return None
    block: set[int] = set()
    s = start_slot
    while s in slots:
        block.add(s)
        s += 1
    return block


def _dev_uuid(device: dict) -> str:
    return (device.get("attributes") or {}).get("uuid", "")
