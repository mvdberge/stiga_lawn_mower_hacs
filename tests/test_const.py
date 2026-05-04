"""Tests for helpers in `custom_components.stiga_mower.const`."""

from __future__ import annotations

from custom_components.stiga_mower.const import split_firmware_version


def test_split_firmware_version_decodes_concatenated_string() -> None:
    # Captured live (2026-04-30): the cloud returns hardware.firmware.build
    # joined with dots, matching matthewgream/stiga-api decodeVersion().
    hw, fw, build = split_firmware_version("0.2.15.0.0.2.15.0.0.0.1.11")
    assert hw == "0.2.15.0"
    assert fw == "0.2.15.0"
    assert build == "0.0.1.11"


def test_split_firmware_version_passes_short_string_through_as_firmware() -> None:
    # Some device types report only one version segment; we cannot tell
    # which slot it belongs to, so we hand it back as the firmware value.
    hw, fw, build = split_firmware_version("1.2.3.4")
    assert hw is None
    assert fw == "1.2.3.4"
    assert build is None


def test_split_firmware_version_handles_none_and_empty() -> None:
    assert split_firmware_version(None) == (None, None, None)
    assert split_firmware_version("") == (None, None, None)


def test_split_firmware_version_rejects_non_numeric_segments() -> None:
    # Defensive: if the cloud ever returns alpha-decorated segments,
    # don't pretend we can split them — pass through as firmware.
    hw, fw, build = split_firmware_version("0.2.15.0.0.2.15a.0.0.0.1.11")
    assert hw is None
    assert fw == "0.2.15.0.0.2.15a.0.0.0.1.11"
    assert build is None
