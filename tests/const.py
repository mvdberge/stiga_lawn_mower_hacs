"""Shared constants for the STIGA test suite."""

from __future__ import annotations

# Synthetic identifiers — not tied to any real account. Real account dumps
# live under tests/fixtures/ and must be anonymised before commit.
TEST_EMAIL = "tester@example.com"
TEST_PASSWORD = "test-pa55word"
TEST_TOKEN = "fake-id-token"
TEST_REFRESH_TOKEN = "fake-refresh-token"

TEST_DEVICE_UUID = "00000000-0000-0000-0000-000000000001"
TEST_DEVICE_NAME = "Bumblebee"
TEST_DEVICE_MAC = "AA:BB:CC:DD:EE:01"
TEST_BASE_UUID = "00000000-0000-0000-0000-000000000002"
TEST_BASE_MAC = "AA:BB:CC:DD:EE:02"
TEST_BROKER_ID = "broker"
