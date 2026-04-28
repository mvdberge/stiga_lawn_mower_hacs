"""Pytest fixtures for the STIGA lawn mower integration."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest

# pytest-homeassistant-custom-component is an optional dev dependency.
# Codec-only test runs (pytest tests/test_protobuf_codec.py) must work
# without it, so we soft-import and only register the HA fixtures when the
# package is present.
try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    _HAS_PHACC = True
except ImportError:  # pragma: no cover - depends on environment
    MockConfigEntry = None  # type: ignore[assignment]
    _HAS_PHACC = False

from .const import TEST_EMAIL, TEST_PASSWORD


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """Make Home Assistant load this custom component during tests.

    No-op when pytest-homeassistant-custom-component is not installed (so
    pure-Python codec/constant tests keep running in minimal envs).
    """
    if not _HAS_PHACC:
        yield
        return
    enable = request.getfixturevalue("enable_custom_integrations")
    _ = enable
    yield


@pytest.fixture
def mock_config_entry():
    """A pre-built ConfigEntry for the integration."""
    if not _HAS_PHACC:
        pytest.skip("pytest-homeassistant-custom-component not installed")
    return MockConfigEntry(
        domain="stiga_mower",
        title=TEST_EMAIL,
        unique_id=TEST_EMAIL,
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        version=1,
    )


@pytest.fixture
def mock_api_authenticate() -> Generator[None, None, None]:
    """Stub StigaAPI.authenticate so tests don't hit Firebase."""
    with patch(
        "custom_components.stiga_mower.api.StigaAPI.authenticate",
        return_value=None,
    ):
        yield
