"""Tests for the STIGA config flow (user / reauth / reconfigure)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.stiga_mower.api import StigaApiError, StigaAuthError
from custom_components.stiga_mower.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

from .const import TEST_EMAIL, TEST_PASSWORD

_DEVICES = [{"attributes": {"uuid": "u1", "name": "Bumblebee", "mac_address": "MAC1"}}]


def _patch_api(*, auth_exc: Exception | None = None, devices: list | None = None):
    """Patch the StigaAPI methods the flow's credential check relies on."""
    return patch.multiple(
        "custom_components.stiga_mower.config_flow.StigaAPI",
        authenticate=AsyncMock(side_effect=auth_exc),
        get_devices=AsyncMock(return_value=_DEVICES if devices is None else devices),
    )


def _patch_setup():
    """Avoid the full integration setup when an entry is created/reloaded."""
    return patch("custom_components.stiga_mower.async_setup_entry", return_value=True)


def _entry(password: str = TEST_PASSWORD) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_EMAIL,
        title=TEST_EMAIL,
        data={CONF_EMAIL: TEST_EMAIL, CONF_PASSWORD: password},
    )


# ---------------------------------------------------------------- user step


@pytest.mark.asyncio
async def test_user_flow_success(hass) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with _patch_api(), _patch_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_EMAIL
    assert result["data"] == {CONF_EMAIL: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD}
    assert result["result"].unique_id == TEST_EMAIL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (StigaAuthError("bad creds"), "invalid_auth"),
        (StigaApiError("network down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_credential_errors(hass, exc: Exception, expected: str) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with _patch_api(auth_exc=exc):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
async def test_user_flow_no_devices(hass) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with _patch_api(devices=[]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_devices"}


@pytest.mark.asyncio
async def test_user_flow_already_configured(hass) -> None:
    _entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with _patch_api():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_user_flow_whitespace_padded_already_configured(hass) -> None:
    """A padded/mixed-case variant of an existing email must still abort."""
    _entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with _patch_api():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: f"  {TEST_EMAIL.upper()}  ", CONF_PASSWORD: TEST_PASSWORD},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_user_flow_normalizes_stored_email(hass) -> None:
    """The created entry stores the stripped, lowercased email."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with _patch_api(), _patch_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: f"  {TEST_EMAIL.upper()}  ", CONF_PASSWORD: TEST_PASSWORD},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_EMAIL
    assert result["data"][CONF_EMAIL] == TEST_EMAIL
    assert result["result"].unique_id == TEST_EMAIL


# ---------------------------------------------------------------- reauth


@pytest.mark.asyncio
async def test_reauth_flow_success(hass) -> None:
    entry = _entry(password="old-password")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with _patch_api(), _patch_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


@pytest.mark.asyncio
async def test_reauth_flow_invalid_auth(hass) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with _patch_api(auth_exc=StigaAuthError("nope")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "whatever"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


# ---------------------------------------------------------------- reconfigure


@pytest.mark.asyncio
async def test_reconfigure_flow_success(hass) -> None:
    entry = _entry(password="old-password")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with _patch_api(), _patch_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: TEST_EMAIL, CONF_PASSWORD: "new-password"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


@pytest.mark.asyncio
async def test_reconfigure_flow_account_mismatch(hass) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "someone-else@example.com", CONF_PASSWORD: "x"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "account_mismatch"
