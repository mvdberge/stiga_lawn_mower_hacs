# Changelog

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
