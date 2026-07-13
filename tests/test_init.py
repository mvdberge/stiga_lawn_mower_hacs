"""Tests for the integration entry point."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.stiga_mower import (
    _build_mqtt,
    _is_real_mac,
    _migrate_keyboard_lock_unique_id,
)


def _device(uuid: str, *, mac: str | None = None, broker_id: str | None = None) -> dict:
    attrs: dict = {"uuid": uuid, "name": uuid}
    if mac is not None:
        attrs["mac_address"] = mac
    if broker_id is not None:
        attrs["broker_id"] = broker_id
    return {"attributes": attrs}


def test_build_mqtt_returns_none_when_no_mac(hass) -> None:
    coordinator = MagicMock()
    coordinator.data = {"devices": [_device("u1")]}  # no mac_address
    api = MagicMock()
    assert _build_mqtt(hass, api, coordinator) is None


def test_build_mqtt_registers_every_robot_with_mac(hass) -> None:
    coordinator = MagicMock()
    coordinator.data = {
        "devices": [
            _device("u1", mac="MAC1", broker_id="acc-7"),
            _device("u2", mac="MAC2", broker_id="acc-7"),
            _device("u3"),  # no mac — skipped
        ],
    }
    api = MagicMock()

    mqtt = _build_mqtt(hass, api, coordinator)

    assert mqtt is not None
    assert set(mqtt._robots) == {"MAC1", "MAC2"}
    assert mqtt.broker_host == "robot-mqtt-acc-7.stiga.com"


def test_build_mqtt_picks_majority_broker_id(hass) -> None:
    coordinator = MagicMock()
    coordinator.data = {
        "devices": [
            _device("u1", mac="MAC1", broker_id="alpha"),
            _device("u2", mac="MAC2", broker_id="beta"),
            _device("u3", mac="MAC3", broker_id="beta"),
        ],
    }
    api = MagicMock()

    mqtt = _build_mqtt(hass, api, coordinator)

    assert mqtt is not None
    # "beta" appears twice → wins the tally.
    assert mqtt.broker_host == "robot-mqtt-beta.stiga.com"


def test_build_mqtt_falls_back_when_no_broker_id(hass) -> None:
    coordinator = MagicMock()
    coordinator.data = {
        "devices": [_device("u1", mac="MAC1")],  # no broker_id at all
    }
    api = MagicMock()

    mqtt = _build_mqtt(hass, api, coordinator)

    assert mqtt is not None
    # Falls back to the literal "broker" the official app uses.
    assert mqtt.broker_host == "robot-mqtt-broker.stiga.com"


def test_is_real_mac_accepts_colon_separated() -> None:
    assert _is_real_mac("aa:bb:cc:dd:ee:ff") is True


def test_is_real_mac_rejects_placeholder_strings() -> None:
    # UBLOXGNSS RTK reference bases report this literal placeholder.
    assert _is_real_mac("UBLOXGNSS") is False
    assert _is_real_mac("") is False
    assert _is_real_mac(None) is False
    assert _is_real_mac(123456) is False


def test_build_mqtt_registers_only_real_base_macs(hass) -> None:
    """Bases with placeholder MACs (UBLOXGNSS) must not be subscribed to."""
    coordinator = MagicMock()
    coordinator.data = {
        "devices": [_device("u1", mac="aa:bb:cc:dd:ee:ff", broker_id="acc-7")],
        "bases": [
            {"mac_address": "UBLOXGNSS"},  # placeholder — skip
            {"mac_address": "11:22:33:44:55:66"},  # real — add_base
            {"mac_address": None},  # missing — skip
        ],
    }
    api = MagicMock()

    mqtt = _build_mqtt(hass, api, coordinator)

    assert mqtt is not None
    assert set(mqtt._bases) == {"11:22:33:44:55:66"}


@pytest.mark.asyncio
async def test_migrate_keyboard_lock_unique_id_renames_legacy_entries(
    hass, mock_config_entry
) -> None:
    """Legacy ``stiga_<uuid>_keyboard_lock`` unique IDs are rewritten to
    ``..._sleep_mode`` so users who enabled the (default-disabled) switch in
    earlier versions keep their entity_id and any automations referencing it.
    """
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)

    legacy = registry.async_get_or_create(
        domain="switch",
        platform="stiga_mower",
        unique_id="stiga_uuid-1_keyboard_lock",
        config_entry=mock_config_entry,
    )
    untouched = registry.async_get_or_create(
        domain="switch",
        platform="stiga_mower",
        unique_id="stiga_uuid-1_anti_theft",
        config_entry=mock_config_entry,
    )

    await _migrate_keyboard_lock_unique_id(hass, mock_config_entry)

    assert registry.async_get(legacy.entity_id).unique_id == "stiga_uuid-1_sleep_mode"
    assert registry.async_get(untouched.entity_id).unique_id == "stiga_uuid-1_anti_theft"


@pytest.mark.asyncio
async def test_migrate_keyboard_lock_unique_id_idempotent(hass, mock_config_entry) -> None:
    """Running the migration twice must not raise — the second pass finds no
    legacy entries and is a no-op.
    """
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        domain="switch",
        platform="stiga_mower",
        unique_id="stiga_uuid-1_keyboard_lock",
        config_entry=mock_config_entry,
    )

    await _migrate_keyboard_lock_unique_id(hass, mock_config_entry)
    await _migrate_keyboard_lock_unique_id(hass, mock_config_entry)
