"""Compliance regression tests for the wire-level invariants documented in
``.claude/CLAUDE.md``.

These tests are intentionally cross-cutting: each one pins down one specific
invariant that, if broken silently, would let the firmware reset user-configured
state on the next write. They complement the per-module tests by tying the
entity descriptions directly to the bundling / encoding guarantees.

Invariants under test:

* **T1** ``coordinator.build_settings_payload`` is the only safe bundling point.
  For every settings-bound entity (switch / select / number with
  ``settings_key``) writing that key must result in a payload that also
  carries the four atomic sibling keys when they are known in
  ``live_settings``.
* **T2** ``mqtt_messages.decode_settings`` never invents sibling-default values
  for fields whose sub-message was absent on the wire. Absence ≠ default-write.
* **T3** ``mqtt_messages.encode_schedule_enabled`` keeps ``enabled`` and
  ``blob`` atomically bundled — omitting ``blob`` must be the caller's explicit
  choice, and every in-tree call site passes ``blob=`` as a kwarg.
* **T4** Every entity write-path calls ``apply_live_settings`` (or
  ``apply_live_schedule`` for the schedule mode) immediately after publishing
  the MQTT command, so the UI does not wait for a SETTINGS frame that may
  silently omit the touched field.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.stiga_mower import mqtt_messages as mm
from custom_components.stiga_mower.coordinator import StigaDataUpdateCoordinator
from custom_components.stiga_mower.number import NUMBER_DESCRIPTIONS
from custom_components.stiga_mower.select import SELECT_DESCRIPTIONS
from custom_components.stiga_mower.switch import SWITCH_DESCRIPTIONS

ATOMIC_SIBLING_KEYS = (
    "rain_sensor_enabled",
    "rain_sensor_delay_h",
    "zone_cutting_height_enabled",
    "cutting_height_mm",
)

# Realistic non-default live_settings used as the backfill source. Picking
# non-default values makes accidental "wrote default 0/False" regressions
# observable.
LIVE_SETTINGS_BASELINE: dict[str, object] = {
    "rain_sensor_enabled": True,
    "rain_sensor_delay_h": 8,
    "zone_cutting_height_enabled": True,
    "cutting_height_mm": 45,
}


@pytest.fixture
async def coordinator(hass) -> StigaDataUpdateCoordinator:
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)
    c._devices = [
        {"attributes": {"uuid": "u1", "name": "Bumblebee", "mac_address": "MAC1"}},
    ]
    c.async_set_updated_data(c._build_data(rest_statuses={"u1": {}}))
    c._on_mqtt_settings("MAC1", dict(LIVE_SETTINGS_BASELINE))
    return c


def _all_settings_keys() -> list[str]:
    """Enumerate every entity description that writes via cmd_settings_update."""
    keys: list[str] = []
    for desc in SWITCH_DESCRIPTIONS:
        if (k := getattr(desc, "settings_key", None)) is not None:
            keys.append(k)
    for desc in SELECT_DESCRIPTIONS:
        if (k := getattr(desc, "settings_key", None)) is not None:
            keys.append(k)
    for desc in NUMBER_DESCRIPTIONS:
        if (k := getattr(desc, "settings_key", None)) is not None:
            keys.append(k)
    return keys


# ---------------------------------------------------------------- T1


@pytest.mark.parametrize("settings_key", _all_settings_keys())
async def test_t1_every_settings_key_bundles_rain_and_cutting(
    coordinator: StigaDataUpdateCoordinator, settings_key: str
) -> None:
    """Every settings-bound entity must go through build_settings_payload such
    that writing its key produces a payload carrying rain + cutting siblings.

    Failure mode this guards against: a new entity is added that writes
    cmd_settings_update directly without bundling, causing the firmware to
    reset rain/cutting to proto3 default on every write.
    """
    # Use a benign dummy value — the helper does not validate values.
    payload = coordinator.build_settings_payload("MAC1", {settings_key: True})

    for sibling in ATOMIC_SIBLING_KEYS:
        assert sibling in payload, (
            f"build_settings_payload({settings_key!r}=…) dropped {sibling!r}; "
            f"firmware would reset it to proto3 default."
        )
        # The caller-supplied key is allowed to coincide with a sibling key
        # (e.g. when the user is actually toggling the rain switch).
        if sibling != settings_key:
            assert payload[sibling] == LIVE_SETTINGS_BASELINE[sibling]


# ---------------------------------------------------------------- T2


def test_t2_decode_empty_frame_yields_empty_dict() -> None:
    """A 0-byte SETTINGS frame must not synthesize any sibling defaults."""
    assert mm.decode_settings(b"") == {}


def test_t2_decode_unrelated_field_only_does_not_emit_siblings() -> None:
    """Frame carrying only push_notifications must not emit cutting/rain delay.

    Specifically rain_sensor_delay_h and cutting_height_mm must be ABSENT from
    the decoded dict (not present-with-default), because writing a default
    value into live_settings would later make encode_settings_update emit an
    explicit ``{field: 0}`` byte sequence and reset firmware state.
    """
    from custom_components.stiga_mower.protobuf_codec import encode

    frame = encode({14: {1: 1}})  # push_notifications=True only
    out = mm.decode_settings(frame)

    # Rain submsg absent → enabled emitted as False (clears prior True), but
    # delay must NOT appear.
    assert out.get("rain_sensor_enabled") is False
    assert "rain_sensor_delay_h" not in out

    # Cutting submsg absent → neither key must appear.
    assert "zone_cutting_height_enabled" not in out
    assert "cutting_height_mm" not in out


# ---------------------------------------------------------------- T3


def test_t3_encode_schedule_enabled_without_blob_omits_field_two() -> None:
    """``blob=None`` must remain the explicit opt-out path — but encoded bytes
    must then contain only field 1, never an implicit empty field 2."""
    from custom_components.stiga_mower.protobuf_codec import decode

    payload = mm.encode_schedule_enabled(True, blob=None)
    # encode_command wraps params under field 2 of the outer command frame.
    outer = decode(payload)
    inner = outer.get(2)
    assert isinstance(inner, dict)
    assert 1 in inner
    assert 2 not in inner


def test_t3_encode_schedule_enabled_with_blob_bundles_both_fields() -> None:
    from custom_components.stiga_mower.protobuf_codec import decode

    payload = mm.encode_schedule_enabled(False, blob=b"\x01\x02\x03")
    outer = decode(payload)
    inner = outer.get(2)
    assert isinstance(inner, dict)
    assert 1 in inner
    assert inner[2] == b"\x01\x02\x03"


def test_t3_every_cmd_schedule_set_enabled_callsite_passes_blob_kwarg() -> None:
    """Static AST check: no in-tree call to cmd_schedule_set_enabled omits the
    ``blob=`` keyword argument.

    Rationale: a positional-only call is a footgun if the encoder ever gains
    additional positional params, and we want the call sites to be visually
    obvious — every reviewer should see that blob is being threaded through.
    """
    from custom_components import stiga_mower

    pkg_root = Path(inspect.getfile(stiga_mower)).parent
    offenders: list[str] = []

    for py_file in pkg_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name != "cmd_schedule_set_enabled":
                continue
            kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            if "blob" not in kw_names:
                offenders.append(f"{py_file.relative_to(pkg_root)}:{node.lineno}")

    assert offenders == [], (
        f"Each cmd_schedule_set_enabled call must pass blob= explicitly; offenders: {offenders}"
    )


# ---------------------------------------------------------------- T4


def test_t4_apply_live_settings_called_for_every_settings_write() -> None:
    """Static AST check: in switch.py / select.py / number.py, every function
    that awaits ``cmd_settings_update`` also calls ``apply_live_settings`` in
    the same function body.

    Guards against a refactor that drops the optimistic update — the UI would
    then appear stuck at the old value until the (default-omitting) SETTINGS
    frame arrives.
    """
    from custom_components import stiga_mower

    pkg_root = Path(inspect.getfile(stiga_mower)).parent
    offenders: list[str] = []

    for filename in ("switch.py", "select.py", "number.py"):
        tree = ast.parse((pkg_root / filename).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            call_names = {c.func.attr if isinstance(c.func, ast.Attribute) else None for c in calls}
            if "cmd_settings_update" in call_names and "apply_live_settings" not in call_names:
                offenders.append(f"{filename}:{node.lineno}:{node.name}")

    assert offenders == [], (
        "Every function that publishes cmd_settings_update must also call "
        f"apply_live_settings in the same body; offenders: {offenders}"
    )


def test_t4_apply_live_schedule_called_after_cmd_schedule_set_enabled() -> None:
    """Same guarantee as T4 above, but for the schedule-mode write path."""
    from custom_components import stiga_mower

    pkg_root = Path(inspect.getfile(stiga_mower)).parent
    offenders: list[str] = []

    for py_file in pkg_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            call_names = {c.func.attr if isinstance(c.func, ast.Attribute) else None for c in calls}
            if "cmd_schedule_set_enabled" not in call_names:
                continue
            if "apply_live_schedule" not in call_names:
                offenders.append(f"{py_file.relative_to(pkg_root)}:{node.lineno}:{node.name}")

    assert offenders == [], (
        "Every function publishing cmd_schedule_set_enabled must also call "
        f"apply_live_schedule; offenders: {offenders}"
    )
