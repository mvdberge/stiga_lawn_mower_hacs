"""STIGA lawn mower integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StigaAPI
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from .coordinator import StigaDataUpdateCoordinator
from .mqtt_client import StigaMQTT
from .schedule_manager import StigaScheduleManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.LAWN_MOWER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type StigaConfigEntry = ConfigEntry[StigaDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: StigaConfigEntry) -> bool:
    """Set up the integration."""
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
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="mqtt_connection_failed",
                translation_placeholders={"error": str(err)},
            )
            mqtt = None

    entry.runtime_data = coordinator
    entry.async_on_unload(_make_unload(mqtt))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Must run after async_forward_entry_setups so the mower device entries
    # are already present in the device registry when we associate the
    # schedule helper entity with the device.
    schedule_manager = StigaScheduleManager(hass, entry, coordinator)
    await schedule_manager.async_setup()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: StigaConfigEntry) -> bool:
    """Unload the integration."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


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

    broker_id = max(set(broker_ids), key=broker_ids.count) if broker_ids else None

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


def _is_real_mac(value: object) -> bool:
    """Heuristic for the STIGA cloud's MAC field.

    UBLOXGNSS RTK bases report `mac_address: "UBLOXGNSS"` instead of an
    actual MAC. A real MAC is six colon-separated octets — we require the
    colon to be present and a minimum length to filter the placeholder.
    """
    return isinstance(value, str) and ":" in value and len(value) >= 11


def _make_unload(mqtt: StigaMQTT | None):
    """Closure that stops the MQTT loop on entry unload."""

    async def _unload() -> None:
        if mqtt is not None:
            await mqtt.stop()

    return _unload
