"""Sanity checks for the constants module.

These tests guard against typos in the manually-curated enum maps. They do
not exercise the broker — that happens in test_mqtt_client.py once the
client lands.
"""

from __future__ import annotations

import pytest

from custom_components.stiga_mower import mqtt_constants as mc


def test_command_name_map_is_invertible() -> None:
    # Every documented command id has a name; the names are unique so the
    # map can be inverted without losing info.
    assert len(set(mc.ROBOT_CMD_NAMES.values())) == len(mc.ROBOT_CMD_NAMES)


def test_critical_command_ids_have_expected_value() -> None:
    """Lock the IDs we hand-craft in encoders to known values."""
    assert mc.ROBOT_CMD_STOP == 0
    assert mc.ROBOT_CMD_START == 1
    assert mc.ROBOT_CMD_GO_HOME == 4
    assert mc.ROBOT_CMD_SETTINGS_UPDATE == 18
    assert mc.ROBOT_CMD_SCHEDULING_SETTINGS_UPDATE == 20
    assert mc.ROBOT_CMD_STATUS_REQUEST == 28


def test_status_type_codes_have_expected_value() -> None:
    assert mc.ROBOT_STATUS_TYPES[0] == "WAITING_FOR_COMMAND"
    assert mc.ROBOT_STATUS_TYPES[4] == "DOCKED"
    assert mc.ROBOT_STATUS_TYPES[13] == "GOING_HOME"
    assert mc.ROBOT_STATUS_TYPES[32] == "CUTTING_BORDER"
    assert mc.ROBOT_STATUS_TYPES[255] == "ERROR"


def test_info_codes_match_firmware_documentation() -> None:
    # Decimal sanity: 0x01A9 = 425 = RAIN_SENSOR per firmware error log
    assert mc.ROBOT_STATUS_INFO_CODES[425] == "RAIN_SENSOR"
    assert mc.ROBOT_STATUS_INFO_CODES[0x01B0] == "LIFT_SENSOR"


def test_cutting_height_map_round_trip() -> None:
    # The wire encoding uses indices 0..8 mapped to mm 20..60 in 5mm steps.
    for mm, idx in mc.CUTTING_HEIGHTS_MM.items():
        assert mc.CUTTING_HEIGHT_INDEX_TO_MM[idx] == mm
    assert sorted(mc.CUTTING_HEIGHTS_MM) == [20, 25, 30, 35, 40, 45, 50, 55, 60]


def test_cutting_modes_match_app_naming() -> None:
    # These four labels match what the STIGA.GO app uses (snake_case for HA compatibility).
    assert set(mc.CUTTING_MODES) == {"dense_grid", "chess_board", "north_south", "east_west"}


def test_rain_delays_map_round_trip() -> None:
    for hours, idx in mc.RAIN_DELAYS_HOURS.items():
        assert mc.RAIN_DELAY_INDEX_TO_HOURS[idx] == hours


def test_base_status_types_have_expected_value() -> None:
    assert mc.BASE_STATUS_TYPES[1] == "STANDBY"
    assert mc.BASE_STATUS_TYPES[5] == "PUBLISHING_CORRECTIONS"


@pytest.mark.parametrize(
    "template",
    [
        mc.ROBOT_TOPIC_LOG_WILDCARD,
        mc.ROBOT_TOPIC_CMD_ROBOT,
        mc.ROBOT_TOPIC_CMD_ACK,
        mc.ROBOT_TOPIC_NOTIFICATION,
        mc.BASE_TOPIC_LOG_WILDCARD,
        mc.BASE_TOPIC_CMD,
        mc.BASE_TOPIC_CMD_ACK,
        mc.BASE_TOPIC_NOTIFICATION,
    ],
)
def test_topic_templates_have_mac_placeholder(template: str) -> None:
    assert "{mac}" in template


def test_broker_host_template_uses_broker_id_placeholder() -> None:
    assert "{broker_id}" in mc.MQTT_BROKER_HOST_TEMPLATE
    rendered = mc.MQTT_BROKER_HOST_TEMPLATE.format(broker_id=mc.MQTT_BROKER_HOST_FALLBACK)
    assert rendered == "robot-mqtt-broker.stiga.com"
