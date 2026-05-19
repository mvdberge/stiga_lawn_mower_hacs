# Python environment
- Use .venv as python environment

# 

# Testing
- Keep all local tests in-line with the code
- Before commiting, perform all local tests
- Run ruff check 
- Run ruff format

# Proto3 default-omission
STIGA firmware uses proto3 encoding: scalar fields at their wire default (0 / False / empty) are **omitted** from SETTINGS frames. This causes two recurring bugs:
- **Read side**: SETTINGS frames are **complete snapshots** of non-default state, not incremental patches. A missing scalar key means "at default"; a missing sub-message means **all** its fields are at default. **Exception for live_settings**: do NOT write a default value into live_settings just because a key is absent from the frame — only write values that were explicitly present in the wire. Reason: writing proto3 defaults into live_settings causes encode commands to include explicit `{field: 0}` bytes which may reset firmware state the user configured (e.g. rain delay). Use `wire_default` on the entity description for display fallbacks instead.
- **Write side**: after a write command, the firmware's SETTINGS response omits any field that transitioned to its default. The coordinator merge cannot detect this. Always call `coordinator.apply_live_settings()` immediately after publishing a MQTT command so the UI reflects the new value without waiting for the (incomplete) response.

## Wire-level verification
Empirically established via `capture/inject_settings.py` (raw capture in `capture/bug2_capture.jsonl`):
- After `SETTINGS_UPDATE` the firmware emits multiple identical `LOG/SETTINGS` frames **unsolicited** — no follow-up `SETTINGS_REQUEST` is needed.
- Each frame is a **complete snapshot** of all non-default fields, not a partial frame containing only the touched field. Absence of a submsg = the entire submsg is at default.
- `cmd_settings_update` is **globally atomic for rain (field 1) and cutting (field 4)**: every outbound write must carry the current rain and cutting submessages, *even when the write touches a completely unrelated submsg* (e.g. `push_notifications` field 14, `obstacle_notifications` field 15, `long_exit`). If rain/cutting are omitted, the firmware resets them to proto3 default. Sibling fields within rain (`enabled`, `delay_h`) and within cutting (`zone_cutting_height_enabled`, `cutting_height_mm`) must likewise be bundled together. Use `coordinator.build_settings_payload(mac, changes)` as the single bundling point — never inline this logic in entity platforms.
- `SCHEDULING_SETTINGS_UPDATE` (cmd 20) is **atomic as a whole**: field 1 (enabled) and field 2 (blob) must always be sent together, otherwise the omitted field is reset to its default. `cmd_schedule_set_enabled(mac, enabled, blob=blob)` is the only safe path.

# Release
- Before release, run "Testing"
- Update CHANGELOG.md, custom_components/stiga_mower/manifest.jso
- Commit and push relevant files
- Create a github release
