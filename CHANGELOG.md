# Changelog

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
