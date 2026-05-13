"""Manages HA schedule helper entities for STIGA mowing schedules.

Creates one ``schedule.*`` helper entity per mower on first setup, then keeps
it bidirectionally in sync:

  MQTT → HA:  coordinator listener detects a changed live_schedule and calls
              ``collection.async_update`` to rewrite the entity's time-blocks.

  HA → MQTT:  a change-set listener on the schedule collection fires whenever
              the user edits the entity in the HA UI; we translate back to the
              STIGA 48-slot bitmap and publish via MQTT.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import StigaDataUpdateCoordinator
from .mqtt_messages import pack_schedule

if TYPE_CHECKING:
    from . import StigaConfigEntry

_LOGGER = logging.getLogger(__name__)

_DAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
_STORAGE_VERSION = 1
_CHANGE_UPDATED = "updated"


class StigaScheduleManager:
    """Creates and syncs HA schedule helper entities for STIGA mowers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: StigaConfigEntry,
        coordinator: StigaDataUpdateCoordinator,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._store = Store(hass, _STORAGE_VERSION, f"stiga_mower.schedules.{entry.entry_id}")
        self._item_ids: dict[str, str] = {}  # mac → schedule storage item ID
        self._last_pushed: dict[str, dict] = {}  # mac → ha_schedule last written to HA
        self._syncing: set[str] = set()  # loop guard: macs currently being synced

    async def async_setup(self) -> None:
        """Create or reuse schedule entities for every known mower."""
        stored = await self._store.async_load() or {}
        self._item_ids = dict(stored)

        for device in self._coordinator.data.get("devices", []):
            attrs = device.get("attributes") or {}
            mac = attrs.get("mac_address")
            if not mac:
                continue
            uuid = attrs.get("uuid") or ""
            name = attrs.get("name") or uuid or mac
            await self._setup_device(mac, uuid, name)

        collection = self._hass.data.get("schedule")
        if collection is not None:
            self._entry.async_on_unload(
                collection.async_add_change_set_listener(self._on_collection_changed)
            )

        self._entry.async_on_unload(
            self._coordinator.async_add_listener(self._on_coordinator_update)
        )

    async def _setup_device(self, mac: str, uuid: str, name: str) -> None:
        """Create or reuse the schedule entity for one mower."""
        collection = self._hass.data.get("schedule")
        if collection is None:
            _LOGGER.warning(
                "HA schedule component not loaded; skipping schedule entity for %s", mac
            )
            return

        item_id = self._item_ids.get(mac)
        if item_id and item_id in collection.data:
            _LOGGER.debug("Reusing existing schedule entity %s for %s", item_id, mac)
        else:
            live = self._coordinator.data.get("live_schedule", {}).get(mac, {})
            ha_sched = _stiga_to_ha(live.get("days") or [])

            item = await collection.async_create(
                {"name": f"Stiga – {name}", "icon": "mdi:robot-mower", **ha_sched}
            )
            item_id = item["id"]
            self._item_ids[mac] = item_id
            self._last_pushed[mac] = ha_sched
            await self._store.async_save(self._item_ids)
            _LOGGER.debug("Created schedule entity %s for %s", item_id, mac)

        # Associate with the mower device — runs as a task so that entity
        # registration (which HA schedules via async_create_task internally)
        # has a chance to complete before we look up the entity registry.
        if uuid:
            self._hass.async_create_task(self._associate_with_device(item_id, uuid))

    async def _associate_with_device(self, item_id: str, uuid: str) -> None:
        """Link the schedule helper entity to the mower device in the registries.

        Retries up to 5 times because HA's schedule component registers
        entities via async_create_task, so the entity registry entry may not
        exist yet when this coroutine first runs.
        """
        dev_reg = dr.async_get(self._hass)
        ent_reg = er.async_get(self._hass)

        for attempt in range(5):
            device = dev_reg.async_get_device(identifiers={(DOMAIN, uuid)})
            if not device:
                _LOGGER.debug("Device %s not yet in registry on attempt %d", uuid, attempt)
            else:
                entity_id = ent_reg.async_get_entity_id("schedule", "schedule", item_id)
                if entity_id is None:
                    for entry in er.async_entries_for_domain(ent_reg, "schedule"):
                        if entry.unique_id == item_id:
                            entity_id = entry.entity_id
                            break

                if entity_id:
                    reg_entry = ent_reg.async_get(entity_id)
                    if reg_entry and reg_entry.device_id != device.id:
                        ent_reg.async_update_entity(entity_id, device_id=device.id)
                        _LOGGER.debug(
                            "Associated schedule entity %s with device %s", entity_id, uuid
                        )
                    return

            await asyncio.sleep(0.5 * (attempt + 1))

        _LOGGER.warning(
            "Could not associate schedule entity %s with device %s after retries", item_id, uuid
        )

    # ------------------------------------------------------ MQTT → HA

    @callback
    def _on_coordinator_update(self) -> None:
        """Push schedule changes received via MQTT into the HA schedule entity."""
        live_schedules = self._coordinator.data.get("live_schedule", {})
        for mac, _item_id in self._item_ids.items():
            if mac in self._syncing:
                continue
            days = live_schedules.get(mac, {}).get("days")
            if days is None:
                continue
            ha_sched = _stiga_to_ha(days)
            if ha_sched == self._last_pushed.get(mac):
                continue
            self._hass.async_create_task(self._push_to_ha(mac, ha_sched))

    async def _push_to_ha(self, mac: str, ha_schedule: dict) -> None:
        item_id = self._item_ids.get(mac)
        if not item_id:
            return
        collection = self._hass.data.get("schedule")
        if collection is None or item_id not in collection.data:
            return
        self._syncing.add(mac)
        try:
            await collection.async_update(item_id, ha_schedule)
            self._last_pushed[mac] = ha_schedule
        finally:
            self._syncing.discard(mac)

    # ------------------------------------------------------ HA → MQTT

    @callback
    def _on_collection_changed(self, change_sets: list[Any]) -> None:
        """Forward user edits on the schedule entity to the STIGA robot."""
        for cs in change_sets:
            if cs.action != _CHANGE_UPDATED:
                continue
            item_id = cs.item_id
            for mac, iid in self._item_ids.items():
                if iid == item_id and mac not in self._syncing:
                    self._hass.async_create_task(self._push_to_mqtt(mac))

    async def _push_to_mqtt(self, mac: str) -> None:
        item_id = self._item_ids.get(mac)
        if not item_id:
            return
        collection = self._hass.data.get("schedule")
        if collection is None or item_id not in collection.data:
            return

        days = _ha_to_stiga(collection.data[item_id])
        blob = pack_schedule(days)

        mqtt = self._coordinator.mqtt
        if mqtt is None or not mqtt.connected:
            _LOGGER.warning("Cannot push schedule for %s: MQTT not connected", mac)
            return

        await mqtt.cmd_schedule_update(mac, blob)
        self._last_pushed[mac] = _stiga_to_ha(days)
        self._coordinator.apply_live_schedule(mac, {"days": days})


# ---------------------------------------------------------------- format helpers


def _slot_to_timestr(slot: int) -> str:
    """30-min slot index → ``HH:MM`` string.  Slot 48 → ``'24:00'``."""
    h, m = divmod(slot * 30, 60)
    return "24:00" if h >= 24 else f"{h:02d}:{m:02d}"


def _timestr_to_slot(s: str) -> int:
    """``HH:MM`` or ``HH:MM:SS`` → exclusive 30-min slot index (max 48)."""
    parts = s.split(":")
    return min(int(parts[0]) * 2 + int(parts[1]) // 30, 48)


def _contiguous_blocks(slots: set[int]) -> list[tuple[int, int]]:
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


def _stiga_to_ha(days: list[dict]) -> dict[str, list[dict]]:
    """STIGA ``[{"slots": set[int]}, ...]`` → HA schedule day dict."""
    result: dict[str, list[dict]] = {}
    for i, day in enumerate(_DAYS):
        slots: set[int] = days[i].get("slots", set()) if i < len(days) else set()
        result[day] = [
            {"from": _slot_to_timestr(s), "to": _slot_to_timestr(e + 1)}
            for s, e in _contiguous_blocks(slots)
        ]
    return result


def _ha_to_stiga(item: dict) -> list[dict]:
    """HA schedule item dict → STIGA ``[{"slots": set[int]}, ...]``."""
    days = []
    for day in _DAYS:
        slots: set[int] = set()
        for block in item.get(day) or []:
            start = _timestr_to_slot(block.get("from", "00:00"))
            end = _timestr_to_slot(block.get("to", "00:00"))
            slots.update(range(start, end))
        days.append({"slots": slots})
    return days
