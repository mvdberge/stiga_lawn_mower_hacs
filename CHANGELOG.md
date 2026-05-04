# Changelog

## [2.2.1] - 2026-05-04

### Added

- `rtk_quality_pct` decoded from STATUS field 19.5 (Survey-In quality, %)

### Fixed

- `signal_quality_pct` now correctly decoded from field 20.3.11 (was previously misread as RSSI). The −32768 modem sentinel keeps the entity unavailable when the value is not reported.

### Removed

- RSSI sensor — value was sourced from the wrong protobuf field; RSRP covers the same information correctly.

## [2.2.0] - 2026-05-03

### Added

- **Reset error** button entity — clear active fault codes from the mower via MQTT

### Fixed

- Status data no longer "sticks" on reconnect — stale values from previous session are cleared during init
- STATUS frame loading issue that caused incorrect field parsing after reconnects

## [2.1.1] - 2026-04-30

### Fixed

- `battery_level` no longer overwritten by field 18.4.1, which is an unknown incrementing counter (not a percentage). Correct value comes from field 17.2.
- `battery_charging` (`Lädt`) no longer incorrectly set to `True` while mowing. Field 18.4.3 is a constant flag (not a charging boolean); charging state is now derived exclusively from `status_type == CHARGING`.

---

## [2.1.0] - 2026-04-30

### Fixed

- Corrected STATUS frame field mapping based on live capture analysis:
  - `battery_voltage`, `battery_current`, `battery_charging` are now decoded from field 18.4 (mowing sub-message), not absent REST data
  - `battery_temp_c` decoded from field 17.7
  - `total_work_time` decoded from field 17.9 (minutes)
  - `rtk_fix_type` decoded from field 19.6 (4 = RTK fixed)
  - Removed erroneous `lat_offset_cm` / `lon_offset_cm` from STATUS frame (position data comes exclusively from the `ROBOT_POSITION` topic)
  - `rssi` correctly decoded from field 20.3.11 with INT16_MIN sentinel filter (−32768 = modem unavailable, suppressed)
  - Removed `signal_quality_pct` from field 20.3.11 (was misidentified)
  - `gps_quality` (field 19.1) is not emitted by this firmware; sensor remains unavailable when absent

---

## [2.0.7] - 2026-04-29

### Fixed

- German translation for `cutting_mode` select: state keys were camelCase (`denseGrid`, `chessBoard`, `northSouth`, `eastWest`) but must be snake_case (`dense_grid`, `chess_board`, `north_south`, `east_west`) to match the option values. German labels were silently falling back to the raw key.
- Sync `strings.json` setup title with `en.json` ("Set up STIGA Robot Mower").

---

## [2.0.6] - 2026-04-29

### Fixed

**"Angedockt", "Lädt", and "Fehler" no longer show Unknown while paused**

When the robot is paused in the field the REST API omits `isDocked`, `battery.charging`, and `errorCode`. All three binary sensors showed "Unknown" (indeterminate state).

- **Angedockt (is_docked)**: MQTT STATUS field 13 (`docking`) is only included when the robot is actively docking; it is absent when standing still. Added a fallback that derives `is_docked` from `status_type`: any type other than `DOCKED` / `CHARGING` resolves to `False`. This runs in `_merge_live_into_status()` whenever a STATUS frame arrives.
- **Lädt (battery_charging)**: The STATUS frame has no dedicated charging boolean. Added inference from `status_type`: charging is `True` only when `status_type == "CHARGING"`, and `False` for all other states (mowing, paused, going home, …). Applied only when REST `battery.charging` is absent.
- **Fehler (error_active)**: An absent `errorCode` means there is no fault — returning `None` (Unknown) was misleading. Changed `is_on` to return `False` directly when `error_code` is missing; `True` only when a non-zero error code is present.

---

## [2.0.5] - 2026-04-29

### Fixed

**Position entity now shows live GPS location**
- The device tracker was reading from `live_position` (populated only by `ROBOT_POSITION` frames), but `request_position()` was never called. The STATUS frame already carries `lat_offset_cm` / `lon_offset_cm` in the same 30-second poll cycle. Changed the tracker to read directly from `statuses[uuid]` — no extra MQTT round-trip needed.

**Settings entities (switches and selects) now populate on connect**
- `live_settings` was always empty because `request_settings()` was never sent. The broker does not push settings spontaneously. Added a one-shot `_request_all_settings()` call immediately after the connection-time `_poll_all_robots()`. Switch and select entities (smart cutting height, rain sensor delay, etc.) now show their current values within seconds of MQTT connection.

### Notes
- **RSSI**: LTE (4G) robots report RSRP and RSRQ but not RSSI (a 2G metric). "Unavailable" for RSSI on LTE robots is expected and correct.
- **GPS quality / RTK quality**: The robot omits the GPS sub-frame when docked without an active GPS fix. These sensors will show "Unavailable" while docked and populate automatically when the robot starts mowing.

---

## [2.0.4] - 2026-04-29

### Fixed

**Network signal sensors now show correct negative dBm/dB values**
- RSSI, RSRP, and RSRQ are LTE signal-strength metrics stored as signed 32-bit integers in the protobuf wire format. The codec was returning them as raw unsigned varints (e.g. RSRP −93 dBm appeared as 4,294,967,203 dBm).
- Signal quality (%) was affected by the same issue.
- **Fix**: Added `_as_signed_int32` helper (mirrors matthewgream's `toInt32`) and applied it to all four network sub-frame fields (`rssi`, `rsrp`, `signal_quality_pct`, `rsrq`) in `decode_status()`.
- **Result**: Network signal sensors now display sensible negative dBm/dB values (e.g. RSRP −93 dBm).

---

## [2.0.3] - 2026-04-29

### Fixed

**MQTT status polling — sensors now receive continuous updates**
- **Root cause**: STIGA robots do not push status frames spontaneously; they must be **polled continuously**. v2.0.2 sent a single STATUS_REQUEST at connection time, but the robot only responded once and then went silent, leaving sensors empty after the first frame (or unavailable if reconnects happened in between).
- **Fix**: Added a background polling task that sends STATUS_REQUEST every 30 seconds for the duration of each MQTT session, matching matthewgream/stiga-api's reference implementation (`timing_levels: status:30s`).
- **Result**: MQTT sensors (zone progress, garden progress, GPS quality, satellites, signal strength, etc.) now update every 30 seconds while MQTT is connected.

### Technical notes
- New constant `MQTT_STATUS_POLL_INTERVAL = 30` (seconds) in `mqtt_constants.py`.
- New methods `_poll_loop()` and `_poll_all_robots()` in `mqtt_client.py`. The poll task is started after subscriptions are established and cleanly cancelled when the MQTT session ends or reconnects.

---

## [2.0.2] - 2026-04-29

### Fixed

**MQTT sensors now receive live data**
- Integration now automatically requests status from robots after establishing MQTT connection. Previously, robots were subscribed to but never asked to send status frames, resulting in all MQTT sensors showing "unavailable".
- Status requests are sent once at connection time and robots continuously stream updates thereafter.

---

## [2.0.1] - 2026-04-29

### Fixed

**MQTT data not flowing to sensors (critical fix)**
- **Root cause**: Integration was connecting to MQTT but **not requesting status from the robots**. MQTT is push-based, but STIGA robots do not emit status frames unless explicitly requested via the `STATUS_REQUEST` command.
- **Fix**: After MQTT subscriptions are established, the integration now sends `request_status()` commands to all connected robots, triggering the first batch of live sensor data. Subsequent status frames flow continuously as robots report their state.
- **Result**: MQTT sensors (zone progress, garden progress, GPS quality, satellites, etc.) now populate within seconds of MQTT connection.

**MQTT sensor availability**
- MQTT-only sensors now correctly show as **unavailable** when MQTT data is absent, instead of remaining **unknown**. Sensors only become available once the first MQTT frame arrives with the expected data fields.
- REST sensors (battery level, cutting height, work time) remain unaffected and continue to display REST data even without MQTT.

**MQTT connection error reporting**
- MQTT startup failures are now reported via Home Assistant's issue registry, surfacing a clear warning message instead of silently falling back to REST-only mode.
- Improved logging: MQTT errors now log at `ERROR` level with detailed context; successful status requests are logged at `DEBUG` level for diagnostics.

### Technical notes
- Added `_MQTT_ONLY_SENSOR_KEYS` constant to explicitly list sensors that require MQTT data.
- Sensor availability logic now distinguishes between MQTT-only fields and REST-provided fields at the `available` property level.
- Added debug logging in `_on_mqtt_status` and `_dispatch_robot_log` to help diagnose future MQTT issues.

---

## [2.0.0] - 2026-04-28

Complete rewrite of the data path: live status and commands now go through direct MQTT cloud communication instead of REST-only polling. The integration class changes from `cloud_polling` to `cloud_push`.

### Added

**MQTT infrastructure**
- Direct mTLS connection to the STIGA MQTT broker (`broker.connectivity-production.stiga.com:8883`) via `aiomqtt`
- Push-driven coordinator: every received MQTT frame calls `async_set_updated_data` immediately — no waiting for the next poll
- Automatic token refresh every 50 minutes (Firebase token TTL is 60 minutes)
- Automatic MQTT reconnect on transient network errors

**New entities (all driven by MQTT live data)**
- `binary_sensor`: Cloud connection, Rain sensor, Lift sensor, Bump sensor, Slope sensor, Lid, Docked, Charging, Error
- `number`: Cutting height (20–60 mm in 5 mm steps, writable)
- `switch`: Rain sensor, Anti-theft, Keyboard lock, Push notifications, Obstacle notifications, Smart cutting height, Long exit
- `select`: Cutting mode (Dense Grid / Chess Board / North-South / East-West), Rain sensor delay (4 h / 8 h / 12 h)
- `device_tracker`: Live GPS position updated from MQTT status frames
- `button`: Calibrate blades, Refresh status
- `calendar`: Mowing Schedule — reads the weekly schedule and exposes each time window as a recurring HA event; supports creating and deleting windows directly from the HA calendar UI, written back to the robot within seconds via MQTT

**Lawn mower improvements**
- Real **Pause** (stop-in-place via MQTT) — previously unavailable through the REST API
- New activity state `returning` when the robot is navigating back to the dock

**Sensor improvements**
- Current zone, Zone progress, Garden progress — live from MQTT
- Diagnostic signal sensors: GPS satellites, RTK quality, GPS quality, RSSI, RSRP, RSRQ, Signal quality

### Changed
- `iot_class` updated from `cloud_polling` to `cloud_push`
- `manifest.json`: added `aiomqtt>=2.3.0` dependency
- Minimum HA version bumped to 2024.4.0
- All existing entity IDs are preserved for backward compatibility

### Technical notes — schedule wire format
STIGA Vista / A15v robots encode the weekly schedule as 7 × 6 protobuf varint values (42 logical bitmask values). Values > 127 take 2 wire bytes, which is why a full schedule blob is 56 bytes rather than the 42 bytes documented for classic A-Series robots in matthewgream/stiga-api. The decoder handles both formats transparently.

---

## [1.7.0] - 2026-04-27
### Added
- Friendly model name in the device registry — e.g. *A 15v* instead of `2R7112028/ST1` (from `/api/devices/{uuid}`)
- Garden perimeter sensors from `/api/perimeters` (all diagnostic):
  - **Garden Area** (m²)
  - **Zones** (count)
  - **Obstacles** (count)
  - **Obstacle Area** (m²)
- New sensor descriptions added in future integration releases now appear automatically on the next coordinator update — no need to remove and re-add the integration

### Changed
- Static metadata (friendly model name + perimeter) is fetched at setup and refreshed every 6 hours, so changes made in the STIGA.GO app (re-drawing the perimeter, renaming the mower) propagate without an integration reload. If those undocumented endpoints are unavailable the corresponding sensors stay unavailable but core functionality is unaffected

---

## [1.6.0] - 2026-04-27
### Added
- **Firmware version** is now reported in the device registry (`sw_version`)
- **MAC address** is now linked to the device record so Home Assistant can correlate the mower with other network entities
- **Cutting Height** sensor (mm, diagnostic) — read-only mirror of the value configured in the STIGA.GO app
- **Total Work Time** sensor (diagnostic, total-increasing)
- New entity attributes on the lawn mower entity: `last_used`, `lte_version`, `total_work_time`, `base_uuid`, `rain_sensor`, `schedule_enabled`
- Coverage for the `ROBOT_ERROR` value of `currentAction` (now mapped to *Error* state)

### Changed
- Device list is now fetched from the richer `/api/garage` endpoint when reachable, with an automatic fallback to the documented `/api/garage/integration` endpoint if the cloud refuses it. The integration continues to work either way

---

## [1.5.0] - 2026-04-27
### Added
- Honour the `hasData` flag from the STIGA cloud — when the mower reports no fresh telemetry, the lawn mower entity and all sensors go *unavailable* instead of showing stale values

### Changed
- Removed the *Pause* feature from the lawn mower entity. STIGA's public REST API has no pause command — `endsession` sends the robot back to the dock, which is the same as *Dock*. Advertising both was misleading
- Cleaned up the `extra_*` state attributes: the nested `battery` payload is no longer echoed as `extra_battery` (its values are already exposed as dedicated sensors)

---

## [1.4.2] - 2026-04-22
### Changed
- Update the brand assets as required by the Home Assistant Brands proxy: `icon.png` (256×256), `icon@2x.png` (512×512), `logo.png` (360×256), `logo@2x.png` (719×512). The previous `icon.png` was 32×32 and displayed pixelated in the integration list.

### Added
- High-DPI brand assets (`icon@2x.png`, `logo@2x.png`) for retina displays

---

## [1.4.1] - 2026-04-22
### Added
- `error_description` state attribute on the lawn mower entity — the numeric `errorCode` is mapped to a human-readable key (e.g. `low_battery`, `lift_sensor`, `out_of_perimeter`) using the info-code table from [matthewgream/stiga-api](https://github.com/matthewgream/stiga-api)
- Coverage for additional `currentAction` states reported by Vista robots: `WAITING_FOR_COMMAND`, `CUTTING_BORDER`, `PLANNING_ONGOING`, `REACHING_FIRST_POINT`, `NAVIGATING_TO_AREA`, `UPDATING`, `STORING_DATA`, `CALIBRATION`, `BLADES_CALIBRATING`, `BLOCKED`, `LID_OPEN`, `STARTUP_REQUIRED`

### Fixed
- Mower state honours the API's `isDocked` flag and new `currentAction` values (`AT_HOME`, `BACK_HOME`, `BACK_HOME_MANUAL`); `mowingMode` values `SCHEDULED` and `IDLE` are no longer hard-wired to *Docked*. When no confirming signal is available, the mower now falls back to *Paused* instead of incorrectly showing *Docked*
- Corrected mapping of `WAITING` / `STOPPED` to *Paused* (not *Docked*) — these indicate the robot is standing outside the charging station waiting for input

---

## [1.4.0] - 2026-04-22
### Added
- Reconfigure flow for updating credentials from the UI
- Diagnostics download (credentials and identifiers redacted)
- New STIGA devices picked up automatically without restart

### Changed
- Battery sensors other than level grouped under the *diagnostic* entity category
- Migrated to `ConfigEntry.runtime_data`
- Set `PARALLEL_UPDATES = 1`
- Quieter logging on transient API errors

---

## [1.3.0] - 2026-04-21
### Added
- Reauthentication flow for handling expired credentials

### Changed
- Refined display precision for sensor values
- Removed redundant sensors to reduce entity clutter
- Brand logo moved to the correct path
- Improved translations

### Fixed
- Raised HomeAssistantError on errors for better error propagation

---

## [1.2.0] - 2026-04-21
### Fixed
- Mower state now correctly uses `currentAction` (what the robot is doing) instead of `mowingMode` (how the session was started), fixing incorrect "docked" state during scheduled mowing sessions
- `BORDER_CUTTING` and other `currentAction` values are now correctly mapped to `mowing`
- Sensor values are now kept from the last successful update instead of dropping to unavailable on transient errors

### Changed
- Device list is fetched once at startup (`_async_setup`) instead of on every 30-second poll, significantly reducing update cycle duration
- Each update cycle now has a hard 25-second timeout to prevent a slow API from blocking the event loop
- Invalid credentials now immediately stop update retries and prompt re-authentication in Home Assistant
- Empty device list from API is now treated as an error instead of silently succeeding
- JSON parse failures in API responses are now logged as warnings

---

## [1.1.0] - 2026-04-21
### Added
- Persistent repair issue in Settings → Repairs after 3 consecutive cloud connection failures (auto-resolves on reconnect)

### Fixed
- API requests now have a 10-second timeout, preventing a slow or unresponsive STIGA server from blocking the update cycle

---

## [1.0.2] - 2026-04-21
### Added
- Plugin icon (STIGA logo)

### Changed
- All code strings and messages translated to English

### Fixed
- Mower incorrectly reported as `docked` while actively mowing:
  - `currentAction` is now used as fallback when `mowingMode` is absent from the API response
  - Unknown mowing modes now result in `unknown` state instead of `docked`

---

## [1.0.1] - 2026-04-21
### Changed
- Updated repository URLs and paths in `manifest.json` and `hacs.json`

---

## [1.0.0] - 2026-04-21
### Added
- Initial release
- Direct cloud integration with the STIGA Integration REST API (no MQTT required)
- `lawn_mower` entity with start, dock, and pause support
- Battery sensors: level, voltage, power, current, runtime, cycles, health, capacity
- Firebase authentication via STIGA.GO app credentials
- HACS support
