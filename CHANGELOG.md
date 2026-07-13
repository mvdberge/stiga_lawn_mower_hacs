# Changelog

## [2.6.0] - 2026-07-17

### Added

- **Mowing schedule as a calendar entity** — the weekly mowing schedule is now a first-class `calendar.*` entity that lives under the mower device. Each active mowing window shows as a recurring event; create or delete windows straight from the Home Assistant calendar UI and they are written back to the mower within seconds. Edits from the STIGA.GO app sync back into the calendar automatically. Windows snap to the mower's 30-minute grid.
- **New "Hibernation" switch** to put the robot to sleep and wake it again from Home Assistant — the same action as the STIGA.GO app's hibernation control. It replaces the old, hidden "Keyboard lock" switch, which never actually locked the keypad. Unlike its predecessor, Hibernation is **enabled by default** so it is available out of the box.
- **New "Active error" sensor** giving the mower's current fault as a single readable value (with the raw error code as an attribute), so you can trigger automations or notifications on specific errors without decoding numeric codes yourself.
- **New position and connectivity sensors** — GPS satellite count, GPS/RTK fix quality and cellular signal metrics (RSRP, RSRQ and overall signal quality) are now surfaced as diagnostic sensors, alongside the dock firmware version. As with the other diagnostic sensors, enable the ones you want from the device page.
- **New "Perform boot" button** to get the mower going again when it is stuck in the "startup required" state — the same action as the STIGA.GO app's "Boot ausführen". The button only becomes actionable while the mower is actually in that state; pressing it runs the startup routine so the mower accepts commands again.

### Changed

- **The mowing schedule is now a calendar entity, replacing the previous `schedule.*` helper entity.** This is a **breaking change**: the old `schedule.<mower>` helper entity is removed and a new `calendar.<mower>` entity takes its place. Any automations, scripts or dashboard cards that referenced the old `schedule.*` entity must be updated to point at the new `calendar.*` entity. Your mowing schedule on the mower itself is untouched — only how Home Assistant exposes it changes.
- **The "Keyboard lock" switch was renamed to "Hibernation".** Home Assistant migrates the entity automatically, so if you had previously enabled it, your existing `entity_id` and any automations referencing it keep working — the meaning of the switch changes from a (non-functional) keypad lock to sleep/wake.

### Fixed

- **Cutting height can no longer be set to an unsupported value.** The mower only accepts heights on a fixed 5 mm grid; entering an off-grid value (e.g. 22 mm) now shows a clear error instead of the UI appearing to accept a value the mower silently drops.
- **More reliable sign-in.** When the cloud session expires the integration now quietly refreshes the access token instead of performing a full re-login, reducing the chance of being asked to sign in again and avoiding a login race that could occur on startup.
- **Steadier live connection.** The real-time (MQTT) connection reconnects more robustly after network interruptions, no longer briefly flips the "Cloud connection" sensor during routine token refreshes, and copes better with unexpected or malformed data from the cloud without dropping the connection.
- **Diagnostics no longer leak identifying details.** The downloadable diagnostics report now redacts your mower's MAC address, the MQTT broker address and your account identifiers, so it is safe to share when reporting issues.
- **More accurate mower state and controls.** Start now goes over the live connection with a REST fallback, mower activity labels map more reliably (including previously unknown states), and MQTT-only sensors correctly show as unavailable when the live connection is down instead of reporting stale values.
- **Live-connection problems are now visible.** If the real-time connection can't be established, a repair notice appears in **Settings → Repairs** (and clears automatically once it recovers), instead of the mower silently staying on slower polling.

## [2.5.1] - 2026-06-01

### Fixed

- Firmware version shown in Home Assistant now updates after a mower firmware upgrade. Previously the `sw_version` in the device registry was frozen at the value seen during integration setup, so flashing a new firmware via STIGA.GO only became visible after a manual integration reload. Each REST poll now checks for a changed `firmware_version` and pushes it to the device registry on the fly.

## [2.5.0] - 2026-05-20

### Removed

- `cutting_mode` select, `device_tracker.position`, `sensor.cutting_height` (writable `number.cutting_height` remains), `button.refresh_status` — low-value entities; position never produced real coordinates on most setups.

### Added

- Base-station REST + MQTT wiring: `/api/garage?relationships=base,connpack` surfaces `included[OwnBases]` into `coordinator.data["bases"]`; `add_base()` is invoked for bases with a real MAC; `decode_base_status` now parses location + network sub-messages; new `decode_base_version` handles `LOG/VERSION` frames. Diagnostics dump exposes `bases`, `live_base_status`, `live_base_version`.

### Fixed

- Spurious `STATUS frame for unregistered robot MAC` warning on every status frame (inner `not {}` check was always true and redundant with the outer dispatch guard).
- README's platform table referenced `device_tracker` and `calendar`, neither of which the integration registers any longer.

### Changed

- Diagnostic battery sensors renamed so every label starts with `Battery` (EN) / `Batterie` (DE) — consistent alphabetical sorting in the HA UI.
- Progress sensors renamed `Zone/Garden Progress` → `Progress Zone/Garden` (DE: `Fortschritt Zone/Garten`) so they sort adjacent.

### Internal

- New `tests/test_compliance.py` codifies the four wire-level invariants from `.claude/CLAUDE.md` as cross-cutting regression tests: every settings-bound entity bundles the atomic rain/cutting siblings; `decode_settings` never invents sibling defaults from absent sub-messages; `encode_schedule_enabled` keeps `enabled`+`blob` paired and every call site passes `blob=` explicitly; every write-path also calls `apply_live_settings` / `apply_live_schedule`.
- Test files restructured 1:1 against source modules: `test_phase5_commands.py` split into per-platform files (`test_switch.py`, `test_select.py`, `test_number.py`, `test_button.py`, `test_lawn_mower.py`); `test_calendar.py` → `test_schedule_manager.py`; `test_sensor_phase4.py` → `test_sensor.py`. Shared entity builders moved to `tests/_entity_helpers.py`.
- Removed dead decode paths for `long_exit_mode` and `zone_cutting_height_uniform` — both were parsed from SETTINGS frames but had no consumer entity and no encoder counterpart.

## [2.4.3] - 2026-05-19

### Fixed

- **Toggling any non-rain/non-cutting setting silently reset rain delay and cutting height** — empirically `cmd_settings_update` is globally atomic for the rain submsg (field 1) and the cutting submsg (field 4): any outbound write that omits these submessages causes the firmware to reset their fields to the proto3 default, *even when the write targets a completely unrelated submsg* (e.g. `push_notifications`, `obstacle_notifications`, `long_exit`). Previously only the rain/cutting entity platforms bundled the sibling fields; flipping any other switch would wipe the configured rain delay back to 4 h and cutting height back to 20 mm. Bundling is now centralized in `coordinator.build_settings_payload()` and applied to every settings write from `switch`, `select` and `number` platforms.

### Internal

- New regression tests in `tests/test_coordinator.py` cover the bundling helper (caller precedence, missing live_settings keys, unrelated-submsg writes).
- `.claude/CLAUDE.md` updated with the global-atomicity finding.

## [2.4.2] - 2026-05-18

### Fixed

- **Editing the mowing schedule silently disabled scheduling mode** — `cmd_schedule_update` only sent field 2 (blob) of `SCHEDULING_SETTINGS_UPDATE`. Because the firmware treats this command as atomic, the omitted field 1 (`enabled`) was reset to its proto3 default (False), so every schedule edit from HA turned auto-mode off. Schedule edits now bundle the current `enabled` state via `cmd_schedule_set_enabled`, mirroring the existing schedule-mode-toggle path.
- **Schedule entity still not appearing in the mower device view** — the previous fix moved setup after platform registration but the HA schedule component itself registers entities via `async_create_task`, so the entity registry entry may not exist yet when device association runs. Fix: device association now runs as an async task with exponential-backoff retries (up to 5 attempts, 0.5 s apart) so it waits for the entity registry entry to appear. Also uses `async_get_entity_id` as a faster primary lookup before falling back to a full domain scan.
- **REST grace timer never expiring on silent API failures** — when every per-device status fetch failed but no exception was raised at the outer level, `_last_rest_success` was still bumped, preventing entities from going unavailable after the 10-minute grace period. The poll is now only considered successful if at least one device's status was actually fetched fresh.
- **`error_active` binary sensor stuck on "Problem" after a fault cleared** — the cloud's REST `errorCode` is sticky and continues to report past faults indefinitely. The coordinator merge only overwrote `error_code` when a live MQTT `info_code` was present, so the stale REST value persisted. Fix: when a live frame omits field 10 (proto3 default = no fault), `error_code` is explicitly cleared.
- **`StigaAPI._post` swallowed every exception while decoding the response** — a bare `except Exception` masked unrelated errors and produced silent `None` returns on malformed JSON. Narrowed to `json.JSONDecodeError` / `aiohttp.ContentTypeError` and added a warning log including the request path and HTTP status.

### Internal

- Verified empirically via the new `capture/inject_settings.py` helper (raw capture in `capture/bug2_capture.jsonl`) that STIGA firmware emits sparse-but-complete `LOG/SETTINGS` snapshots after `SETTINGS_UPDATE`, not partial frames containing only the touched field. Misleading comment in `coordinator._on_mqtt_settings` corrected; `.claude/CLAUDE.md` gained a wire-level reference; two regression tests with captured raw bytes added.

## [2.4.1] - 2026-05-13

### Fixed

- **Schedule entity not appearing in the mower device view** — the schedule helper entity was created before `async_forward_entry_setups` completed, so the mower device did not yet exist in the device registry when the entity was associated. On subsequent restarts the existing entity was reused but association was never re-attempted. Fix: schedule manager setup now runs after platform setup, and device association via the entity registry is attempted on every startup (new and existing entities alike).

## [2.4.0] - 2026-05-13

### Added

- **Native HA Schedule entity for mowing schedule** — the integration now creates a `schedule.*` helper entity per mower on first setup. The entity is fully editable via the built-in Home Assistant Schedule UI (horizontal time-block editor per weekday). Changes made in the UI are pushed to the robot via MQTT; schedule changes received from the robot (e.g. via the STIGA.GO app) are synced back into the HA entity automatically. The previous `calendar.*` entity has been removed.

### Fixed

- **Switching mowing mode could wipe the firmware schedule blob** — `SCHEDULING_SETTINGS_UPDATE` is atomic: omitting field 2 (the schedule blob) resets it to the proto3 default (empty). The mode-switch command now always bundles field 2, even when no schedule data is known locally (fallback: empty blob), so the command never silently clears the stored mowing times.

## [2.3.15] - 2026-05-12

### Fixed

- **Switching to automatic schedule mode wipes all mowing times** — `SCHEDULING_SETTINGS_UPDATE` is atomic on the firmware: sending only field 1 (enabled flag) without field 2 (the schedule blob) resets the blob to the proto3 default (empty), deleting every stored mowing window. Verified against a 2026-05-12 app capture: the STIGA GO app always bundles both fields together (`{1: flag, 2: blob}`). Fix: `async_select_option` now reads the current `days` from `live_schedule`, packs the blob with `pack_schedule`, and passes it alongside the enabled flag — the same atomic-sub-message pattern as the rain-sensor / cutting-height fixes.

## [2.3.14] - 2026-05-12

### Fixed

- **Rain sensor toggle resets cutting height to 20 mm** — the cutting sub-message (field 4) is an atomic write on the firmware: omitting field 4.2 (`cutting_height_mm`) resets it to the proto3 default (index 0 = 20 mm). When toggling the rain sensor switch or changing the rain delay select from HA, the MQTT write already bundled `zone_cutting_height_enabled` (field 4.1) to prevent zone mode from resetting, but omitted field 4.2. Fix: `cutting_height_mm` is now also read from `live_settings` and bundled alongside `zone_cutting_height_enabled` in all rain-related writes, completing the cutting sub-message so the firmware preserves the user-configured height.

## [2.3.13] - 2026-05-12

### Fixed

- **Rain sensor delay reset to 4 h every time rain is disabled** — the 2.3.12 fix always emitted `rain_sensor_delay_h: 4` when the rain sub-message was absent from a SETTINGS frame. Because rain-disabled frames always omit the rain sub-message (disabled is the proto3 wire default), any app-triggered or periodic SETTINGS frame silently overwrote a user-configured 8 h or 12 h delay with 4 h in `live_settings`. The next enable command from HA then sent `{delay_index: 0}` explicitly, resetting the firmware. Fix: `decode_settings` now only emits `rain_sensor_delay_h` when the rain sub-message is actually present on the wire; when absent, only `rain_sensor_enabled: False` is emitted. The delay select falls back to its `wire_default = 4` for display without touching `live_settings`.
- **Cutting height change silently disables zone-based cutting mode** — the cutting sub-message (field 4) is an atomic write on the firmware: omitting field 4.1 (`zone_cutting_height_enabled`) resets it to its proto3 default (False). When changing cutting height from HA, the write previously sent only `{4: {2: height_index}}` without field 4.1, silently disabling zone mode with every height change. Fix: the current `zone_cutting_height_enabled` value is now bundled alongside the height (mirroring all captured app behaviour). An optimistic `apply_live_settings` call is also added so the new height shows immediately even when it equals the proto3 default (20 mm = index 0, which the firmware omits from its response).
- **Scheduled mowing mode stays on "auto" after switching to "manual" from HA** — `schedule enabled=False` is the proto3 default and gets omitted from the firmware's `SCHEDULING_SETTINGS` response. The coordinator merge `{**old, **new}` cannot detect the transition and leaves `live_schedule["enabled"]` as `True`, keeping the select stuck on "auto". Fix: `async_select_option` now calls `apply_live_schedule` immediately after publishing the command, the same optimistic-update pattern used for switches and selects since 2.3.11.

## [2.3.12] - 2026-05-12

### Fixed

- **Rain sensor switch stays "on" when disabled via the STIGA.GO app** — when the official app disables the rain sensor, the firmware sends a SETTINGS frame that entirely omits the rain sub-message (disabled is the proto3 wire default). The decoder previously treated an absent rain sub-message as "frame did not touch rain" and left the old `rain_sensor_enabled: True` value in `live_settings` unchanged. Fix: `decode_settings` now treats an absent rain sub-message as "rain is at its proto3 default" and always writes `rain_sensor_enabled: False` / `rain_sensor_delay_h: 4` for any non-empty SETTINGS frame. The coordinator's merge then overwrites the stale value and the HA switch turns off correctly.

## [2.3.11] - 2026-05-12

### Fixed

- **Rain sensor switch stays "active" after turning it off in HA** — after sending a `SETTINGS_UPDATE` to disable the rain sensor, the STIGA firmware returns a SETTINGS frame that **omits** the rain sub-message entirely (disabled = proto3 wire default = 0, so the field is suppressed). The coordinator's merge-on-receive logic cannot detect this transition: no rain key arrives → `rain_sensor_enabled` stays `True` in `live_settings` → switch shows "active". Fix: write-entities now call `coordinator.apply_live_settings` immediately after publishing a MQTT command, so the UI reflects the new state before the (incomplete) firmware response arrives. The same optimistic update applies to all boolean switches and the rain-delay select (protects against the 4 h default being omitted after a delay change).

## [2.3.10] - 2026-05-12

### Fixed

- **Rain sensor switch and delay select sent incomplete SETTINGS_UPDATE frames** — the STIGA firmware treats the rain sub-message (field 1) and the cutting sub-message (field 4) as atomic writes: any sub-field omitted in the update is reset to its proto3 default (0). Three related bugs: (1) enabling/disabling the rain sensor did not include the current delay index → delay silently reset to 4 h; (2) changing the rain delay via the select did not include the current enabled flag → rain sensor silently disabled; (3) neither write included `zone_cutting_height_enabled` (field 4.1) alongside the rain update → zone-based cutting height was reset. All three writes now read the current values from `live_settings` and bundle the full atomic group before publishing, mirroring the official STIGA.GO app behaviour verified by live capture. `encode_settings_update` additionally gains support for the `zone_cutting_height_enabled` key (previously unimplemented, silently ignored on write).

## [2.3.9] - 2026-05-12

### Fixed

- **Rain delay "4 hours" set in STIGA.GO app not reflected in HA** — a two-layer proto3 bug prevented the 4 h setting from propagating. Layer 1: the codec returned `b''` for an empty LEN submessage payload instead of `{}`, causing the rain sub-message to be silently skipped when all its scalar fields were at their proto3 wire defaults (rain_sensor_delay index 0 = 4 h, omitted by firmware). Layer 2: the decoder only populated rain delay/enabled keys when the rain sub-message was a non-empty dict, so a fully-defaulted rain block was invisible. Both layers are now fixed: the codec maps an empty LEN payload to `{}` (distinguishing "submsg present, all fields at defaults" from "submsg absent"), and the decoder defaults missing inner fields to their proto3 defaults (`0`).

## [2.3.8] - 2026-05-11

### Fixed

- **"Rain Delay" select permanently "Unavailable"** — same root cause as the 2.3.7 switch fix, but missed for the select. The rain delay is encoded on the wire as an enum index (4 h → 0, 8 h → 1, 12 h → 2), and proto3 omits scalar fields whose value equals the wire default. As soon as the robot sat at the factory default of 4 hours, the SETTINGS frame did not carry field 2 at all, so `rain_sensor_delay_h` never appeared in `live_settings` and the select went unavailable. The select now treats a populated `live_settings` entry as "available" and falls back to `4` (the proto3 default) when the key is missing.
- **Mowing progress sensors flickered to "Unavailable" at the start of a cycle** — `current_zone`, `zone_completed_pct` and `garden_completed_pct` come from the mowing sub-message of the STATUS frame, which is also proto3-encoded. A wire value of 0 (zone 0, 0 % completed at the start of mowing) was omitted by the firmware, so the sensors briefly disappeared until the value moved off zero. When the mowing sub-message is present the decoder now defaults missing fields to 0; when the sub-message is entirely absent (idle / docked) the sensors stay correctly unavailable.

### Changed

- **"Cutting Mode: Auto" switch replaced by "Mowing Mode" select** — the manual/scheduled toggle is now a select with two clearly labelled options ("Manual" / "Auto", German "Manuell" / "Automatik") instead of an on/off switch. It also moves from the **Configuration** category into the **Controls** category, matching the lawn-mower's other operating controls. **Migration note:** the old `switch.<mower>_cutting_mode_auto` (German `switch.<mower>_schnittmodus_automatik`) will appear as an orphan entry in the entity registry after the upgrade — remove it via the HA UI. The new entity is `select.<mower>_mowing_mode` (German `select.<mower>_schnittmodus`).

## [2.3.7] - 2026-05-11

### Fixed

- **All switches permanently "Unavailable"** — STIGA's firmware encodes SETTINGS replies with standard proto3 semantics, which omits boolean fields whose value is `False` from the wire. The decoder only added a key when the field was present, so any currently-disabled setting (anti_theft, keyboard_lock, push_notifications, obstacle_notifications, smart_cutting_height, long_exit, often rain_sensor_enabled) never appeared in `live_settings[mac]`, and the switch's availability check `_current_value() is not None` returned `None` → permanently unavailable. The switch now treats a populated `live_settings` entry as "available" and defaults missing keys to `False`, matching the proto3 wire default. Same fix applied to the schedule switch's `enabled` flag.
- **Partial SETTINGS/SCHEDULE frames wiped previously-known state** — after every `cmd_settings_update` write, STIGA replies with a frame containing only the touched field. The coordinator was replacing the entire `live_settings[mac]` entry, so the first write erased every other known setting and flipped all dependent switches to unavailable. `_on_mqtt_settings` and `_on_mqtt_schedule` now merge incoming partial frames with previously-known state.
- **Sensors briefly "Unavailable" during REST hiccups while MQTT was alive** — binary sensor and sensor availability now also accepts MQTT as a live source (battery_level, is_docked, battery_charging, error_code etc. are all refreshed via `_merge_live_into_status`), so entities stay visible during transient REST timeouts as long as MQTT frames are arriving.
- **STIGA cloud timeouts surfaced as raw `TimeoutError`** — `StigaAPI` now wraps `asyncio.TimeoutError` from auth/GET/POST calls into `StigaApiError` with a descriptive message, so the coordinator's retry/backoff logic engages instead of letting the exception propagate.

## [2.3.6] - 2026-05-09

### Fixed

- **Switches were permanently "Unavailable"** — availability was incorrectly tied to the presence of `live_settings`, which was too restrictive. Switches now become available when REST data is fresh and MQTT is connected, regardless of whether SETTINGS_REQUEST responses have been received. This allows switches to function even during transient MQTT issues while the robot is reachable via REST.

### Fixed

- **Further improvements to caching behavior** — additional adjustments to stabilize sensor availability during API errors.

## [2.3.4] - 2026-05-09

### Fixed

- **Sensors become unavailable after 10 minutes of persistent REST errors** — the `_last_rest_success` timestamp was incorrectly updated on API errors, preventing cached data from being marked stale after 10 minutes. Now the timestamp is only updated on successful fetches, so entities correctly become unavailable during continuous failures.

## [2.3.3] - 2026-05-09

### Fixed

- **`schedule_enabled` switch always "Unavailable"** — during MQTT connection setup, a `SETTINGS_REQUEST` was sent but no `SCHEDULING_SETTINGS_REQUEST`. This left `live_schedule` empty and the schedule switch permanently unavailable. Now the schedule is queried from the robot on every new MQTT session.

## [2.3.2] - 2026-05-09

### Added

- **Mowing schedule switch** — new switch entity "Mowing Schedule" (`schedule_enabled`) to toggle between manual and scheduled operation. Sends only the `enabled` flag via MQTT (SCHEDULING_SETTINGS_UPDATE field 1), without modifying the stored schedule blob on the robot. Corresponds to the "manual/scheduled" toggle in the STIGA GO app.

## [2.3.1] - 2026-05-09

### Fixed

- **REST failures no longer mark sensors as "Unavailable"** — on timeouts or errors from the STIGA Cloud API (30–90 s delays or no response), the integration retains the last known state instead of immediately setting all entities to "Unavailable". Sensors remain visible until the cloud has been unreachable for more than 10 minutes.
- **MQTT sensors remain available independently of REST errors** — fields delivered via MQTT (current zone, percentage, RSSI, etc.) remain available as long as MQTT frames are received, even if the REST poll fails.
- **Exponential backoff on MQTT reconnect** — on repeated connection failures to the broker, the wait time grows from 5 s to a maximum of 5 minutes, instead of retrying in a tight loop.

## [2.3.0] - 2026-05-09

### Added

- **Per-zone area sensors** — one diagnostic sensor per zone (`Zone 1 Area`, `Zone 2 Area`, …) showing the mowed area in m², with `num_points` as an extra attribute. Sensors are created dynamically from the `/api/perimeters` response and appear as soon as perimeter data is available.
- **Zone details as attributes** — the existing `Zones` sensor now exposes `zone_N_area_m2` as extra state attributes for a quick overview of all zone sizes.

## [2.2.4] - 2026-05-04

### Fixed

- Removed the **`total_work_time`** sensor — it was sourced from MQTT STATUS field 17.9, which we now believe to be live `battery_remaining_capacity_mAh`, not a cumulative work-time counter. Evidence: the value is a float that fluctuates with battery state, lives inside the BATTERY sub-message alongside capacity/level/temp/current, and matches REST `remainingCapacity` in magnitude. matthewgream's reference decoder does not label that field at all, so the previous "total work time" reading was an unfounded guess from a single capture.
- MQTT field 17.9 now feeds the existing **`battery_remaining`** sensor (mAh, integer), which previously only had stale REST values.

### Changed

- The **`Docked`** binary sensor (`is_docked`) is now reported with `device_class: presence` instead of `occupancy`. State labels change from "Occupied/Free" to "Home/Away".

## [2.2.3] - 2026-05-04

### Changed

- `hw_version` is now only populated when it differs from the firmware version. On most devices the cloud reports the same value in both protobuf VERSION slots, so the duplicate entry was noise.

## [2.2.2] - 2026-05-04

### Fixed

- Robot firmware version is no longer shown as the concatenated 12-segment cloud string. The REST `firmware_version` is split into its three sub-versions (hardware, firmware, build) and only the firmware part is surfaced as `sw_version`; the hardware part is exposed as `hw_version` (matches matthewgream/stiga-api `decodeVersion`).

### Added

- New diagnostic sensor `dock_firmware` exposing the docking-station firmware (`attributes.settings[0].docking_version`) so it is no longer conflated with the robot's own firmware. Disabled by default.

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
- `battery_charging` (`Charging`) no longer incorrectly set to `True` while mowing. Field 18.4.3 is a constant flag (not a charging boolean); charging state is now derived exclusively from `status_type == CHARGING`.

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
