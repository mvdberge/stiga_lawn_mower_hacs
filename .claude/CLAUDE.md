# Python environment
- Use .venv as python environment

# Testing
- Keep all local tests in-line with the code
- Before commiting, perform all local tests
- Run ruff check 
- Run ruff format

# Proto3 default-omission
STIGA firmware uses proto3 encoding: scalar fields at their wire default (0 / False / empty) are **omitted** from SETTINGS frames. This causes two recurring bugs:
- **Read side**: SETTINGS frames are **complete snapshots** of non-default state, not incremental patches. A missing scalar key means "at default"; a missing sub-message means **all** its fields are at default. Never treat absence as "this frame didn't touch these keys" — always default-in missing keys and sub-messages.
- **Write side**: after a write command, the firmware's SETTINGS response omits any field that transitioned to its default. The coordinator merge cannot detect this. Always call `coordinator.apply_live_settings()` immediately after publishing a MQTT command so the UI reflects the new value without waiting for the (incomplete) response.

# Release
- Before release, run "Testing"
- Update CHANGELOG.md, custom_components/stiga_mower/manifest.jso
- Commit and push relevant files
- Create a github release
