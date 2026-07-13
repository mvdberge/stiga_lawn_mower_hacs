"""STIGA lawn mower integration for Home Assistant."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StigaAPI
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from .coordinator import StigaDataUpdateCoordinator
from .mqtt_client import StigaMQTT

_KEYBOARD_LOCK_SUFFIX = "_keyboard_lock"
_SLEEP_MODE_SUFFIX = "_sleep_mode"

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.LAWN_MOWER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type StigaConfigEntry = ConfigEntry[StigaDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: StigaConfigEntry) -> bool:
    """Set up the integration."""
    await _migrate_keyboard_lock_unique_id(hass, entry)

    session = async_get_clientsession(hass)
    api = StigaAPI(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        session=session,
    )

    coordinator = StigaDataUpdateCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    # MQTT requires the device list (MAC + broker_id), so we wire it up
    # *after* the first REST refresh. A failure here must not break the
    # integration — REST polling alone keeps the entities populated.
    mqtt = _build_mqtt(hass, api, coordinator)
    if mqtt is not None:
        coordinator.attach_mqtt(mqtt)
        try:
            await mqtt.start()
        except Exception as err:
            from homeassistant.helpers import issue_registry as ir

            _LOGGER.error("Failed to start STIGA MQTT client: %s; continuing REST-only", err)
            ir.async_create_issue(
                hass,
                DOMAIN,
                "mqtt_connection_failed",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="mqtt_connection_failed",
                translation_placeholders={"error": str(err)},
            )
            mqtt = None

    entry.runtime_data = coordinator
    entry.async_on_unload(_make_unload(mqtt))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: StigaConfigEntry) -> bool:
    """Unload the integration."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: StigaConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow deleting a device the STIGA account no longer reports.

    Returns True (allow removal) when none of the device's identifiers match a
    currently-known mower, so a mower removed from the STIGA.GO account can be
    cleaned up from the Home Assistant UI instead of lingering forever.
    """
    coordinator = config_entry.runtime_data
    known_ids = {
        (DOMAIN, uuid)
        for device in (coordinator.data or {}).get("devices", [])
        if (uuid := (device.get("attributes") or {}).get("uuid"))
    }
    return not any(identifier in known_ids for identifier in device_entry.identifiers)


def _build_mqtt(
    hass: HomeAssistant,
    api: StigaAPI,
    coordinator: StigaDataUpdateCoordinator,
) -> StigaMQTT | None:
    """Construct an MQTT client and register every known robot.

    Returns ``None`` when no robot has a usable MAC address (the broker
    indexes topics by MAC, so without one there is nothing to subscribe
    to). The selected ``broker_id`` is the most-common value across all
    robots; the STIGA cloud assigns the same id per account in practice.
    """
    devices = (coordinator.data or {}).get("devices", [])
    macs: list[str] = []
    broker_ids: list[str] = []
    for device in devices:
        attrs = device.get("attributes") or {}
        mac = attrs.get("mac_address")
        if not mac:
            continue
        macs.append(mac)
        if bid := attrs.get("broker_id"):
            broker_ids.append(bid)

    if not macs:
        _LOGGER.info(
            "No STIGA robot has a MAC address — skipping MQTT setup, "
            "REST polling will continue to work",
        )
        return None

    unique_brokers = set(broker_ids)
    broker_id = max(unique_brokers, key=broker_ids.count) if broker_ids else None
    if len(unique_brokers) > 1:
        _LOGGER.warning(
            "Robots report differing MQTT brokers %s; connecting only to the "
            "majority broker %s. Robots on other brokers will not receive MQTT "
            "updates (REST polling still works for them).",
            sorted(unique_brokers),
            broker_id,
        )

    mqtt = StigaMQTT(hass, api.get_token, broker_id=broker_id)
    for mac in macs:
        mqtt.add_robot(mac)
    # Register paired base stations so {base_mac}/LOG/+ frames get dispatched.
    # `/api/garage` `included[OwnBases]` populates the real MAC for richer
    # bases (Vision Cam, Smart Base); plain UBLOXGNSS RTK references report
    # the literal string "UBLOXGNSS" as mac_address, which would result in an
    # impossible MQTT topic, so we skip anything that isn't a colon-separated
    # MAC. Without a real MAC nothing can ever arrive over MQTT.
    for base in (coordinator.data or {}).get("bases", []):
        base_mac = base.get("mac_address")
        if _is_real_mac(base_mac):
            mqtt.add_base(base_mac)
    return mqtt


async def _migrate_keyboard_lock_unique_id(hass: HomeAssistant, entry: StigaConfigEntry) -> None:
    """Rewrite legacy ``*_keyboard_lock`` unique IDs to ``*_sleep_mode``.

    Field 2 of the SETTINGS protobuf was historically labelled ``keyboard_lock``
    (matching matthewgream/stiga-api), but wire-level capture on 2026-06-02
    proved it is actually the firmware's sleep/wake toggle: status_type
    transitions to 54 ("sleeping") after the app sends field 2 = 1, and back
    to 4 ("docking") after field 2 = 0. The switch entity has been renamed
    accordingly. This migration keeps the entity registry entry — and therefore
    the user-visible entity_id used in automations — stable across the rename.
    """

    @callback
    def _update(entity: er.RegistryEntry) -> dict[str, str] | None:
        if entity.unique_id.endswith(_KEYBOARD_LOCK_SUFFIX):
            new_uid = entity.unique_id[: -len(_KEYBOARD_LOCK_SUFFIX)] + _SLEEP_MODE_SUFFIX
            return {"new_unique_id": new_uid}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, _update)


def _is_real_mac(value: object) -> bool:
    """Heuristic for the STIGA cloud's MAC field.

    UBLOXGNSS RTK bases report `mac_address: "UBLOXGNSS"` instead of an
    actual MAC. A real MAC is six colon-separated octets — we require the
    colon to be present and a minimum length to filter the placeholder.
    """
    return isinstance(value, str) and ":" in value and len(value) >= 11


def _make_unload(mqtt: StigaMQTT | None) -> Callable[[], Coroutine[Any, Any, None]]:
    """Closure that stops the MQTT loop on entry unload."""

    async def _unload() -> None:
        if mqtt is not None:
            await mqtt.stop()

    return _unload
