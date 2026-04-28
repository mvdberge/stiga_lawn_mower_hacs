"""Tests for the integration entry point."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.stiga_mower import _build_mqtt


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
