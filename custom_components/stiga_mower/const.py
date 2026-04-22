"""Constants for the STIGA lawn mower integration."""

DOMAIN = "stiga_mower"

CONF_EMAIL    = "email"
CONF_PASSWORD = "password"

UPDATE_INTERVAL  = 30  # seconds
REQUEST_TIMEOUT  = 10  # seconds per HTTP request

# Firebase Auth (publicly embedded in STIGA app code)
FIREBASE_API_KEY  = "AIzaSyCPtRBU_hwWZYsguHp9ucGrfNac0kXR6ug"
FIREBASE_AUTH_URL = "https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword"

# STIGA Cloud API
STIGA_BASE_URL = "https://connectivity-production.stiga.com/api"
EP_GARAGE      = "/garage/integration"
EP_STATUS      = "/devices/{uuid}/mqttstatus"
EP_START       = "/devices/{uuid}/command/startsession"
EP_STOP        = "/devices/{uuid}/command/endsession"

# Device attributes (from official API documentation)
ATTR_SERIAL_NUMBER   = "serial_number"
ATTR_PRODUCT_CODE    = "product_code"
ATTR_DEVICE_TYPE     = "device_type"
ATTR_MOWING_MODE_RAW = "mowing_mode_raw"
ATTR_BATTERY_VOLTAGE = "battery_voltage_v"
ATTR_BATTERY_CAPACITY    = "battery_capacity_mah"
ATTR_BATTERY_REMAINING   = "battery_remaining_mah"
ATTR_BATTERY_CYCLES      = "battery_cycles"
ATTR_BATTERY_POWER       = "battery_power_w"
ATTR_BATTERY_HEALTH      = "battery_health_pct"
ATTR_BATTERY_TIME_LEFT   = "battery_time_left_min"
ATTR_BATTERY_CURRENT     = "battery_current_a"
ATTR_ERROR_CODE          = "error_code"
ATTR_ERROR_DESCRIPTION   = "error_description"

# Error / status info codes. Cross-checked against
# https://github.com/matthewgream/stiga-api (ROBOT_STATUS_INFO_CODES).
ERROR_INFO_CODES: dict[int, str] = {
    0x0064: "low_battery",
    0x0191: "blocked",
    0x0195: "unknown_0195",
    0x019E: "unknown_019e",
    0x01A2: "lid_sensor",
    0x01A9: "rain_sensor",
    0x01B0: "lift_sensor",
    0x01B1: "bump_sensor",
    0x01B2: "slope_sensor",
    0x01B3: "trapped",
    0x01FA: "docking_error",
    0x0389: "wheel_trouble",
    0x03EF: "surface_too_slippery",
    0x03F0: "out_of_perimeter",
}
