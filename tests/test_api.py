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


# ---------------------------------------------------------------- _get retry logic


def _resp(status: int, body=None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body if body is not None else {"ok": True})
    return resp


def _mock_session_get_sequence(items: list) -> MagicMock:
    """Session whose .get yields one item per call: a response or an exception.

    A response item is returned from __aenter__; an Exception item is raised
    from __aenter__ (mimicking a connection-level failure mid-request).
    """
    session = MagicMock()
    calls = iter(items)

    def _next_ctx(*_args, **_kwargs):
        item = next(calls)
        ctx = MagicMock()
        if isinstance(item, BaseException):
            ctx.__aenter__ = AsyncMock(side_effect=item)
        else:
            ctx.__aenter__ = AsyncMock(return_value=item)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session.get = MagicMock(side_effect=_next_ctx)
    return session


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the retry backoff delay in tests."""
    monkeypatch.setattr("custom_components.stiga_mower.api.asyncio.sleep", AsyncMock())


async def test_get_retries_then_succeeds_on_connection_drop() -> None:
    """A dropped connection is retried and the next good response wins."""
    session = _mock_session_get_sequence(
        [aiohttp.ServerDisconnectedError(), _resp(200, {"data": 1})]
    )
    api = _build_api(session)
    assert await api._get("/x") == {"data": 1}
    assert session.get.call_count == 2


async def test_get_retries_on_5xx_then_succeeds() -> None:
    """A transient 5xx is retried before surfacing."""
    session = _mock_session_get_sequence([_resp(503), _resp(200, {"data": 2})])
    api = _build_api(session)
    assert await api._get("/x") == {"data": 2}
    assert session.get.call_count == 2


async def test_get_gives_up_after_exhausting_retries() -> None:
    """Persistent failures raise StigaApiError after all attempts (3 total)."""
    from custom_components.stiga_mower.api import StigaApiError

    session = _mock_session_get_sequence([aiohttp.ClientOSError()] * 3)
    api = _build_api(session)
    with pytest.raises(StigaApiError):
        await api._get("/x")
    assert session.get.call_count == 3


async def test_get_does_not_retry_4xx() -> None:
    """A 4xx is a caller error and must surface immediately, no retry."""
    from custom_components.stiga_mower.api import StigaApiError

    session = _mock_session_get_sequence([_resp(404)])
    api = _build_api(session)
    with pytest.raises(StigaApiError):
        await api._get("/x", retry=False)
    assert session.get.call_count == 1


async def test_get_does_not_retry_timeout() -> None:
    """Timeouts already consumed the budget; they surface without retrying."""
    from custom_components.stiga_mower.api import StigaApiError

    session = _mock_session_get_sequence([TimeoutError()])
    api = _build_api(session)
    with pytest.raises(StigaApiError):
        await api._get("/x")
    assert session.get.call_count == 1


async def test_get_wraps_malformed_json_body() -> None:
    """A 200 with a non-JSON body raises StigaApiError, not a raw JSONDecodeError."""
    from custom_components.stiga_mower.api import StigaApiError

    bad = _resp(200)
    bad.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "", 0))
    session = _mock_session_get_sequence([bad, bad, bad])
    api = _build_api(session)
    with pytest.raises(StigaApiError):
        await api._get("/x")
    assert session.get.call_count == 3


async def test_get_token_double_checked_lock_single_login() -> None:
    """Concurrent get_token() callers trigger exactly one Firebase login."""
    import asyncio as _asyncio

    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"idToken": "T", "expiresIn": "3600"})
    session = _mock_session_post(resp)
    api = StigaAPI(email=TEST_EMAIL, password=TEST_PASSWORD, session=session)

    tokens = await _asyncio.gather(api.get_token(), api.get_token(), api.get_token())

    assert tokens == ["T", "T", "T"]
    assert session.post.call_count == 1


async def test_authenticate_wraps_malformed_json_body() -> None:
    """A non-JSON auth response surfaces as StigaApiError, not a raw decode error."""
    from custom_components.stiga_mower.api import StigaApiError

    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "", 0))
    session = _mock_session_post(resp)
    api = StigaAPI(email=TEST_EMAIL, password=TEST_PASSWORD, session=session)
    with pytest.raises(StigaApiError):
        await api.authenticate()
