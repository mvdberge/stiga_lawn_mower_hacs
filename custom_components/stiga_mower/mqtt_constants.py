"""MQTT topics, command IDs, and enum maps for the STIGA cloud broker.

All values mirror matthewgream/stiga-api (`StigaAPIElements.js`,
`StigaAPIConnectionMQTT.js`, `StigaAPICertificates.js`). Field numbers and
enum values are reverse-engineered, not officially documented — keep this
module the single source of truth and update it whenever new firmware
introduces new codes.
"""

from __future__ import annotations

# ---------------------------------------------------------------- Broker

# `robot-mqtt-{broker}.stiga.com`. The fallback id "broker" is what the app
# uses when no per-device id is set in the REST garage payload (`broker_id`).
MQTT_BROKER_HOST_TEMPLATE = "robot-mqtt-{broker_id}.stiga.com"
MQTT_BROKER_HOST_FALLBACK = "broker"
MQTT_BROKER_PORT = 8883
MQTT_BROKER_USERNAME = "firebaseauth|connectivity-production.stiga.com"

# Bundled mTLS credentials (extracted by matthewgream from the official
# STIGA.GO app). The cert is tied to the broker CN and identical for every
# client; STIGA does not enforce per-device credentials.
MQTT_CERT_FILE = "stiga.crt"
MQTT_KEY_FILE = "stiga.key"

# matthewgream uses `rejectUnauthorized: false`; we mirror this with
# `CERT_NONE` + `check_hostname=False`. The broker presents a self-signed
# cert that is not chained to any public root.
MQTT_VERIFY_SERVER_CERT = False

MQTT_KEEPALIVE = 60  # seconds
MQTT_RECONNECT_DELAY = 5  # seconds, matches matthewgream's reconnectPeriod
MQTT_TOKEN_REFRESH_INTERVAL = 50 * 60  # refresh Firebase id_token before 1h expiry

# STIGA robots do not push status frames spontaneously — they must be polled.
# matthewgream uses 30s for both docked and undocked states; we mirror that.
MQTT_STATUS_POLL_INTERVAL = 30  # seconds

# ---------------------------------------------------------------- Robot topics

# Subscribed by the client (handler dispatches by suffix).
ROBOT_TOPIC_LOG_WILDCARD = "{mac}/LOG/+"
ROBOT_TOPIC_CMD_ROBOT = "{mac}/CMD_ROBOT"
ROBOT_TOPIC_CMD_ACK = "CMD_ROBOT_ACK/{mac}"
ROBOT_TOPIC_NOTIFICATION = "{mac}/JSON_NOTIFICATION"

# Suffixes seen on `{mac}/LOG/<suffix>`.
ROBOT_LOG_STATUS = "STATUS"
ROBOT_LOG_VERSION = "VERSION"
ROBOT_LOG_SETTINGS = "SETTINGS"
ROBOT_LOG_SCHEDULING = "SCHEDULING_SETTINGS"
ROBOT_LOG_POSITION = "ROBOT_POSITION"

# ---------------------------------------------------------------- Robot commands

# Field 1 of the `{mac}/CMD_ROBOT` frame. Values labelled `UNKNOWN_*` are
# emitted by the official app but their semantics are not yet reverse
# engineered — keep them around so we don't accidentally repurpose the id.
ROBOT_CMD_STOP = 0
ROBOT_CMD_START = 1
ROBOT_CMD_UNKNOWN_2 = 2
ROBOT_CMD_GO_HOME = 4
ROBOT_CMD_ZONE_SETTINGS_UPDATE = 7
ROBOT_CMD_SETTINGS_REQUEST = 17
ROBOT_CMD_SETTINGS_UPDATE = 18
ROBOT_CMD_SCHEDULING_SETTINGS_REQUEST = 19
ROBOT_CMD_SCHEDULING_SETTINGS_UPDATE = 20
ROBOT_CMD_VERSION_REQUEST = 21
ROBOT_CMD_POSITION_REQUEST = 22
ROBOT_CMD_CALIBRATE_BLADES = 26
ROBOT_CMD_STATUS_REQUEST = 28
ROBOT_CMD_CLOUDSYNC_REQUEST = 32
ROBOT_CMD_UNKNOWN_37 = 37
ROBOT_CMD_ZONE_ORDER_UPDATE = 47

ROBOT_CMD_NAMES: dict[int, str] = {
    ROBOT_CMD_STOP: "STOP",
    ROBOT_CMD_START: "START",
    ROBOT_CMD_UNKNOWN_2: "UNKNOWN_CMD_2",
    ROBOT_CMD_GO_HOME: "GO_HOME",
    ROBOT_CMD_ZONE_SETTINGS_UPDATE: "ZONE_SETTINGS_UPDATE",
    ROBOT_CMD_SETTINGS_REQUEST: "SETTINGS_REQUEST",
    ROBOT_CMD_SETTINGS_UPDATE: "SETTINGS_UPDATE",
    ROBOT_CMD_SCHEDULING_SETTINGS_REQUEST: "SCHEDULING_SETTINGS_REQUEST",
    ROBOT_CMD_SCHEDULING_SETTINGS_UPDATE: "SCHEDULING_SETTINGS_UPDATE",
    ROBOT_CMD_VERSION_REQUEST: "VERSION_REQUEST",
    ROBOT_CMD_POSITION_REQUEST: "POSITION_REQUEST",
    ROBOT_CMD_CALIBRATE_BLADES: "CALIBRATE_BLADES",
    ROBOT_CMD_STATUS_REQUEST: "STATUS_REQUEST",
    ROBOT_CMD_CLOUDSYNC_REQUEST: "CLOUDSYNC_REQUEST",
    ROBOT_CMD_UNKNOWN_37: "UNKNOWN_CMD_37",
    ROBOT_CMD_ZONE_ORDER_UPDATE: "ZONE_ORDER_UPDATE",
}

# Field 2 of the ACK frame. `1` is the only value matthewgream documents.
ROBOT_CMD_ACK_OK = 1

# ---------------------------------------------------------------- Robot status

# Field 3 of `LOG/STATUS`.
ROBOT_STATUS_TYPES: dict[int, str] = {
    0: "WAITING_FOR_COMMAND",
    1: "MOWING",
    3: "CHARGING",
    4: "DOCKED",
    5: "UPDATING",
    6: "BLOCKED",
    8: "LID_OPEN",
    13: "GOING_HOME",
    18: "CALIBRATION",
    20: "BLADES_CALIBRATING",
    24: "UNKNOWN_24",
    27: "STORING_DATA",
    28: "PLANNING_ONGOING",
    29: "REACHING_FIRST_POINT",
    30: "NAVIGATING_TO_AREA",
    32: "CUTTING_BORDER",
    252: "STARTUP_REQUIRED",
    255: "ERROR",
}

# Field 10.1 of `LOG/STATUS` — error/info code (hex literals match the
# numeric codes used by the firmware).
ROBOT_STATUS_INFO_CODES: dict[int, str] = {
    0x0064: "LOW_BATTERY",
    0x0191: "BLOCKED",
    0x0195: "UNKNOWN_0195",
    0x019E: "UNKNOWN_019E",
    0x01A2: "LID_SENSOR",
    0x01A9: "RAIN_SENSOR",
    0x01B0: "LIFT_SENSOR",
    0x01B1: "BUMP_SENSOR",
    0x01B2: "SLOPE_SENSOR",
    0x01B3: "TRAPPED",
    0x01FA: "DOCKING_ERROR",
    0x0389: "WHEEL_TROUBLE",
    0x03EF: "SURFACE_TOO_SLIPPERY",
    0x03F0: "OUT_OF_PERIMETER",
}

# Subset of info codes that map to a physical sensor binary state.
ROBOT_INFO_CODE_TO_SENSOR: dict[int, str] = {
    0x01A2: "lid_sensor",
    0x01A9: "rain_sensor",
    0x01B0: "lift_sensor",
    0x01B1: "bump_sensor",
    0x01B2: "slope_sensor",
}

# Field 19.1 — GNSS coverage.
ROBOT_GPS_QUALITY: dict[int, str] = {
    0: "GOOD",
    1: "POOR",
    2: "BAD",
    3: "WORSE",
}

# ---------------------------------------------------------------- Settings

# Field map for `SETTINGS_UPDATE` (cmd 18). Value spec mirrors the
# `encodeRobotSettings` reference; nested fields use `(parent, child)`.
# The codec converts booleans to 0/1 automatically.

# Allowed cutting heights and their on-wire indices.
CUTTING_HEIGHTS_MM: dict[int, int] = {
    20: 0,
    25: 1,
    30: 2,
    35: 3,
    40: 4,
    45: 5,
    50: 6,
    55: 7,
    60: 8,
}
CUTTING_HEIGHT_INDEX_TO_MM: dict[int, int] = {v: k for k, v in CUTTING_HEIGHTS_MM.items()}

# Rain delay (hours -> wire index).
RAIN_DELAYS_HOURS: dict[int, int] = {4: 0, 8: 1, 12: 2}
RAIN_DELAY_INDEX_TO_HOURS: dict[int, int] = {v: k for k, v in RAIN_DELAYS_HOURS.items()}

# Cutting modes (name -> wire index).
CUTTING_MODES: dict[str, int] = {
    "dense_grid": 0,
    "chess_board": 1,
    "north_south": 5,
    "east_west": 6,
}
CUTTING_MODE_INDEX_TO_NAME: dict[int, str] = {v: k for k, v in CUTTING_MODES.items()}

# ---------------------------------------------------------------- Schedule
#
# Two distinct wire formats exist depending on robot generation:
#
# A-Series (classic autonomous_robot) — matthewgream reference implementation:
#   Field 2 = 42 raw bytes  (7 days × 6 bytes per day).
#   Each byte is stored literally; values are always ≤ 255 and written as-is.
#   The 42-byte length is therefore constant regardless of schedule content.
#   Source: matthewgream/stiga-api StigaAPIElements.js `decodeScheduleTimes`.
#
# Vista/A15v (vista_robot) — confirmed by live capture 2026-04-28:
#   Field 2 = 7 days × 6 varint-encoded bitmap values = 42 logical values,
#   but the wire length varies because values > 127 occupy 2 bytes each.
#   Example: 0xC0 (=192) → wire bytes 0xC0 0x01; 0xE3 (=227) → 0xE3 0x01.
#   A fully-active schedule produces up to 84 wire bytes; an empty one 42.
#   The captured frame for "11:00–13:00 and 14:30–16:30 every day" was 56 bytes.
#
# Slot semantics are identical in both formats:
#   bit N of bitmap byte M = slot (M*8 + N); slot × 30 min = wall-clock time.
#   48 slots per day cover 00:00–23:30 in 30-minute increments.
#
# This integration implements the Vista format. The A-Series format is
# byte-compatible with the Vista format for all schedules whose bitmap bytes
# happen to be ≤ 127 (i.e. fewer than 8 active slots overlap in one byte);
# in practice the varint decoder handles both transparently because a single-
# byte varint is identical to the raw byte.

SCHEDULE_DAYS = 7
SCHEDULE_TIME_BYTES = 6  # varint values per day = 6 bitmap bytes = 48 slots
SCHEDULE_SLOTS_PER_DAY = SCHEDULE_TIME_BYTES * 8  # 48 slots = 24 h
SCHEDULE_SLOT_MINUTES = 30

# ---------------------------------------------------------------- Base topics

BASE_TOPIC_LOG_WILDCARD = "{mac}/LOG/+"
BASE_TOPIC_CMD = "{mac}/CMD_REFERENCE"
BASE_TOPIC_CMD_ACK = "CMD_REFERENCE_ACK/{mac}"
BASE_TOPIC_NOTIFICATION = "{mac}/JSON_NOTIFICATION"

BASE_LOG_STATUS = "STATUS"
BASE_LOG_VERSION = "VERSION"

# ---------------------------------------------------------------- Base commands

BASE_CMD_VERSION_REQUEST = 1
BASE_CMD_UNKNOWN_3 = 3
BASE_CMD_UNKNOWN_4 = 4
BASE_CMD_UNKNOWN_5 = 5
BASE_CMD_PUBLISH_START = 6
BASE_CMD_PUBLISH_STOP = 7
BASE_CMD_STATUS_REQUEST = 8
BASE_CMD_UNKNOWN_13 = 13
BASE_CMD_SETTINGS_UPDATE = 15

BASE_CMD_NAMES: dict[int, str] = {
    BASE_CMD_VERSION_REQUEST: "VERSION_REQUEST",
    BASE_CMD_UNKNOWN_3: "UNKNOWN_3",
    BASE_CMD_UNKNOWN_4: "UNKNOWN_4",
    BASE_CMD_UNKNOWN_5: "UNKNOWN_5",
    BASE_CMD_PUBLISH_START: "PUBLISH_START",
    BASE_CMD_PUBLISH_STOP: "PUBLISH_STOP",
    BASE_CMD_STATUS_REQUEST: "STATUS_REQUEST",
    BASE_CMD_UNKNOWN_13: "UNKNOWN_13",
    BASE_CMD_SETTINGS_UPDATE: "SETTINGS_UPDATE",
}

# ---------------------------------------------------------------- Base status

BASE_STATUS_TYPES: dict[int, str] = {
    1: "STANDBY",
    2: "INITIALIZING",
    3: "ERROR",
    4: "ACQUIRING_GPS",
    5: "PUBLISHING_CORRECTIONS",
}

BASE_STATUS_FLAGS: dict[int, str] = {
    0: "INACTIVE",
    1: "ACTIVE_OK",
    2: "WARNING",
    3: "ERROR",
}

BASE_LED_MODES: dict[str, int] = {
    "off": 0,
    "always": 1,
    "scheduled": 2,
}
BASE_LED_MODE_INDEX_TO_NAME: dict[int, str] = {v: k for k, v in BASE_LED_MODES.items()}
