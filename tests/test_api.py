"""Tests for the StigaAPI REST helper layer."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.stiga_mower.api import StigaAPI

from .const import TEST_EMAIL, TEST_PASSWORD


def _mock_session_post(response: MagicMock) -> MagicMock:
    """Wrap a response mock in an aiohttp-style async context manager."""
    session = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=ctx)
    return session


def _build_api(session: MagicMock) -> StigaAPI:
    api = StigaAPI(email=TEST_EMAIL, password=TEST_PASSWORD, session=session)
    api._token = "fake-token"  # bypass authenticate()
    return api


@pytest.mark.asyncio
async def test_post_returns_none_for_204_without_body():
    """An empty 204 must short-circuit to None without invoking json()."""
    resp = MagicMock()
    resp.status = 204
    resp.content_length = 0
    resp.json = AsyncMock(side_effect=AssertionError("json() must not be called"))
    api = _build_api(_mock_session_post(resp))

    result = await api._post("/fake")

    assert result is None
    resp.json.assert_not_called()


@pytest.mark.asyncio
async def test_post_logs_warning_and_returns_none_on_invalid_json(caplog):
    """A 200 with malformed JSON body must log a warning and return None."""
    resp = MagicMock()
    resp.status = 200
    resp.content_length = 42
    resp.json = AsyncMock(side_effect=json.JSONDecodeError("Expecting value", doc="", pos=0))
    api = _build_api(_mock_session_post(resp))

    with caplog.at_level(logging.WARNING, logger="custom_components.stiga_mower.api"):
        result = await api._post("/fake")

    assert result is None
    assert any(
        "non-JSON body" in record.message and "/fake" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_post_logs_warning_on_content_type_mismatch(caplog):
    """A 200 with non-JSON content type must log a warning and return None."""
    resp = MagicMock()
    resp.status = 200
    resp.content_length = 2
    resp.json = AsyncMock(
        side_effect=aiohttp.ContentTypeError(
            request_info=MagicMock(),
            history=(),
            message="text/plain",
        )
    )
    api = _build_api(_mock_session_post(resp))

    with caplog.at_level(logging.WARNING, logger="custom_components.stiga_mower.api"):
        result = await api._post("/fake")

    assert result is None
    assert any("non-JSON body" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_post_returns_parsed_json_on_success():
    """Happy path: 200 with a JSON body returns the parsed dict."""
    resp = MagicMock()
    resp.status = 200
    resp.content_length = 17
    resp.json = AsyncMock(return_value={"ok": True})
    api = _build_api(_mock_session_post(resp))

    result = await api._post("/fake")

    assert result == {"ok": True}


# ---------------------------------------------------------------- _extract_bases


def test_extract_bases_from_garage_included() -> None:
    """OwnBases entries in `included[]` are surfaced with the JSONAPI `id`
    promoted to `uuid` when missing from `attributes`."""
    raw = {
        "data": [{"type": "devices", "id": "dev-1"}],
        "included": [
            {
                "type": "OwnBases",
                "id": "base-id-1",
                "attributes": {
                    "product_code": "UBLOXGNSS",
                    "serial_number": "UBLOXGNSS",
                    "mac_address": "UBLOXGNSS",
                    "firmware_version": None,
                    "broker_id": None,
                },
            },
            {
                "type": "ConnPacks",
                "id": "pack-1",
                "attributes": {"status": "active"},
            },
        ],
    }
    bases = StigaAPI._extract_bases(raw)
    assert len(bases) == 1
    assert bases[0]["uuid"] == "base-id-1"
    assert bases[0]["mac_address"] == "UBLOXGNSS"
    assert bases[0]["product_code"] == "UBLOXGNSS"


def test_extract_bases_returns_empty_for_missing_included() -> None:
    assert StigaAPI._extract_bases({"data": []}) == []
    assert StigaAPI._extract_bases([]) == []
    assert StigaAPI._extract_bases(None) == []
