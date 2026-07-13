"""Async client for the STIGA Integration API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from .const import (
    EP_DEVICE,
    EP_GARAGE,
    EP_GARAGE_FULL,
    EP_PERIMETER,
    EP_START,
    EP_STATUS,
    EP_STOP,
    FIREBASE_API_KEY,
    FIREBASE_AUTH_URL,
    REQUEST_RETRIES,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    STIGA_BASE_URL,
)

_LOGGER = logging.getLogger(__name__)


class StigaAuthError(Exception):
    """Authentication error."""


class StigaApiError(Exception):
    """General API error."""


class StigaAPI:
    """
    Async REST client for the STIGA Integration API.

    Authentication via Firebase verifyPassword (idToken).
    All STIGA endpoints are authorized with a Bearer token.
    """

    def __init__(self, email: str, password: str, session: aiohttp.ClientSession) -> None:
        self._email = email
        self._password = password
        self._session = session
        self._token: str | None = None
        # Monotonic deadline after which the cached token is considered stale.
        self._token_expiry: float = 0.0
        # Serialises logins so concurrent callers can't interleave/double-login.
        self._auth_lock = asyncio.Lock()

    # ------------------------------------------------------------------ Auth

    async def _login(self) -> None:
        """Perform the Firebase password login. Caller must hold ``_auth_lock``.

        Kept private and lock-free so both the cache-aware ``get_token`` and the
        401 recovery path (``authenticate``) can drive it without re-entering the
        non-reentrant lock.
        """
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        try:
            async with self._session.post(
                FIREBASE_AUTH_URL,
                json={
                    "email": self._email,
                    "password": self._password,
                    "returnSecureToken": True,
                },
                params={"key": FIREBASE_API_KEY},
                timeout=timeout,
            ) as resp:
                try:
                    data = await resp.json()
                except (json.JSONDecodeError, aiohttp.ContentTypeError) as err:
                    raise StigaApiError(
                        f"Authentication returned a non-JSON body (HTTP {resp.status})"
                    ) from err
                if not isinstance(data, dict):
                    raise StigaApiError("Authentication returned an unexpected body")
                if resp.status != 200:
                    msg = (data.get("error") or {}).get("message", str(resp.status))
                    raise StigaAuthError(f"Authentication failed: {msg}")
                token = data.get("idToken")
                if not token:
                    raise StigaAuthError("Authentication response contained no idToken")
                self._token = token
                # Firebase id-tokens are valid ~1 h; refresh a minute early
                # so we never use a token that expires mid-request.
                try:
                    expires_in = float(data.get("expiresIn", 3600))
                except (TypeError, ValueError):
                    expires_in = 3600.0
                self._token_expiry = time.monotonic() + max(expires_in - 60.0, 60.0)
                _LOGGER.debug("Firebase authentication successful.")
        except aiohttp.ClientError as err:
            raise StigaApiError(f"Network error during authentication: {err}") from err
        except TimeoutError as err:
            raise StigaApiError(f"Timeout during authentication (>{REQUEST_TIMEOUT}s)") from err

    async def authenticate(self) -> None:
        """Force a fresh Firebase login (used for the 401 re-auth retry).

        Serialised behind ``_auth_lock``. If a concurrent caller already
        replaced the (rejected) token while we waited for the lock, skip the
        redundant login so several parallel requests that 401 at once do not
        trigger a re-login storm.
        """
        prev = self._token
        async with self._auth_lock:
            if self._token is not None and self._token != prev:
                return
            await self._login()

    async def get_token(self) -> str:
        """Return a valid Firebase id-token, re-authenticating only when needed.

        The MQTT client uses this as its credential provider and calls it on
        every reconnect (~every 50 minutes). We cache the token until shortly
        before its expiry so routine reconnects don't trigger a redundant
        Firebase login (which risks ``TOO_MANY_ATTEMPTS`` rate-limiting).
        """
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        async with self._auth_lock:
            # Double-checked: another caller may have refreshed the token while
            # we were waiting for the lock — return theirs instead of logging in.
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            await self._login()
        if not self._token:
            raise StigaAuthError("authenticate() returned without a token")
        return self._token

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _get(self, path: str, retry: bool = True) -> Any:
        if not self._token:
            await self.authenticate()
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        last_err: StigaApiError | None = None
        for attempt in range(REQUEST_RETRIES + 1):
            try:
                async with self._session.get(
                    f"{STIGA_BASE_URL}{path}",
                    headers=self._auth_header(),
                    timeout=timeout,
                ) as resp:
                    if resp.status == 401 and retry:
                        _LOGGER.debug("Token expired – re-authenticating.")
                        await self.authenticate()
                        return await self._get(path, retry=False)
                    # 5xx are transient cloud-side hiccups; retry before giving
                    # up so a brief outage doesn't flap entities. 4xx are caller
                    # errors and surface immediately.
                    if resp.status >= 500:
                        last_err = StigaApiError(f"GET {path} → HTTP {resp.status}")
                    elif resp.status != 200:
                        raise StigaApiError(f"GET {path} → HTTP {resp.status}")
                    else:
                        try:
                            return await resp.json()
                        except (json.JSONDecodeError, aiohttp.ContentTypeError) as err:
                            # HTTP 200 but a truncated/garbage body — the cloud
                            # emits these during instability. Treat as transient:
                            # retry, then surface as StigaApiError so the
                            # coordinator keeps cached data instead of crashing.
                            last_err = StigaApiError(f"GET {path} returned a non-JSON body: {err}")
            except aiohttp.ClientError as err:
                # Connection-level drops (ServerDisconnectedError, ClientOSError,
                # ConnectionResetError…) are exactly the "connection broke"
                # symptom; a quick retry usually rides over them.
                last_err = StigaApiError(f"Network error: {err}")
            except TimeoutError as err:
                # A timeout already consumed REQUEST_TIMEOUT seconds; retrying
                # risks blowing the coordinator's per-cycle budget.
                raise StigaApiError(f"Timeout on GET {path} (>{REQUEST_TIMEOUT}s)") from err

            if attempt < REQUEST_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                _LOGGER.debug("Retrying GET %s (attempt %d): %s", path, attempt + 2, last_err)

        raise last_err or StigaApiError(f"GET {path} failed after {REQUEST_RETRIES + 1} attempts")

    async def _post(self, path: str, body: Any = None, retry: bool = True) -> Any:
        if not self._token:
            await self.authenticate()
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        last_err: StigaApiError | None = None
        for attempt in range(REQUEST_RETRIES + 1):
            try:
                async with self._session.post(
                    f"{STIGA_BASE_URL}{path}",
                    headers=self._auth_header(),
                    json=body,
                    timeout=timeout,
                ) as resp:
                    if resp.status == 401 and retry:
                        await self.authenticate()
                        return await self._post(path, body, retry=False)
                    # 5xx are transient cloud-side hiccups; retry before giving
                    # up. 4xx are caller errors and surface immediately. The
                    # start/end-session commands are idempotent, so replaying a
                    # POST after a transient failure is safe.
                    if resp.status >= 500:
                        last_err = StigaApiError(f"POST {path} → HTTP {resp.status}")
                    elif resp.status not in (200, 204):
                        raise StigaApiError(f"POST {path} → HTTP {resp.status}")
                    elif resp.status == 204 or not resp.content_length:
                        return None
                    else:
                        try:
                            return await resp.json()
                        except (json.JSONDecodeError, aiohttp.ContentTypeError) as err:
                            _LOGGER.warning(
                                "POST %s returned non-JSON body (HTTP %d): %s",
                                path,
                                resp.status,
                                err,
                            )
                            return None
            except aiohttp.ClientError as err:
                # Connection-level drops (ServerDisconnectedError, ClientOSError,
                # ConnectionResetError…) are the "connection broke" symptom; a
                # quick retry usually rides over them.
                last_err = StigaApiError(f"Network error: {err}")
            except TimeoutError as err:
                # A timeout already consumed REQUEST_TIMEOUT seconds; retrying
                # risks blowing the coordinator's per-cycle budget.
                raise StigaApiError(f"Timeout on POST {path} (>{REQUEST_TIMEOUT}s)") from err

            if attempt < REQUEST_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                _LOGGER.debug("Retrying POST %s (attempt %d): %s", path, attempt + 2, last_err)

        raise last_err or StigaApiError(f"POST {path} failed after {REQUEST_RETRIES + 1} attempts")

    # ------------------------------------------------------------------ Devices

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return the device list, preferring the richer /garage endpoint.

        The undocumented `/garage` returns the same structure as the
        documented `/garage/integration` but with extra attributes such as
        `firmware_version`, `mac_address`, `base_uuid`, `total_work_time`,
        `last_used` and `parsedSettings`. We use it when available and fall
        back to the official endpoint otherwise.
        """
        try:
            raw = await self._get(EP_GARAGE_FULL)
            devices = self._extract_devices(raw)
            if devices:
                return devices
            _LOGGER.debug("/garage returned no devices, falling back to /garage/integration")
        except StigaApiError as err:
            _LOGGER.debug("/garage unavailable (%s) – using /garage/integration", err)

        raw = await self._get(EP_GARAGE)
        return self._extract_devices(raw)

    @staticmethod
    def _extract_devices(raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("Data", "data", "devices", "robots", "items"):
                if isinstance(raw.get(key), list):
                    return list(raw[key])
            # Treat a bare dict as a single device only if it actually looks like
            # one. A degraded 200-body such as `{"error": ...}` must not be
            # surfaced as a phantom device that overwrites the cached list.
            if "attributes" in raw:
                return [raw]
        return []

    async def get_bases(self) -> list[dict[str, Any]]:
        """Return base-station records from /garage `included[OwnBases]`.

        Each entry is the flat `attributes` dict (uuid, mac_address,
        product_code, serial_number, firmware_version, created_at,
        broker_id, …) with `uuid` filled from the JSONAPI `id`. The cloud
        only emits `included[]` when the request asks for relationships
        explicitly — `/api/garage` alone omits the section even though
        every device has a populated `base_uuid`. Undocumented endpoint;
        returns `[]` when /garage is unavailable.
        """
        try:
            raw = await self._get(f"{EP_GARAGE_FULL}?relationships=base,connpack")
        except StigaApiError as err:
            _LOGGER.debug("/garage unavailable for bases extraction: %s", err)
            return []
        bases = self._extract_bases(raw)
        if not bases and isinstance(raw, dict):
            # Help diagnose responses that come back without the expected
            # OwnBases type — different accounts may report bases under a
            # different `type` (e.g. richer Vision Cam / Smart Base SKUs).
            included = raw.get("included")
            if isinstance(included, list):
                types = sorted(
                    {
                        t
                        for i in included
                        if isinstance(i, dict) and (t := i.get("type")) is not None
                    }
                )
                _LOGGER.debug("/garage included[] types: %s", types or "<none>")
            else:
                _LOGGER.debug("/garage response has no included[] section")
        return bases

    @staticmethod
    def _extract_bases(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, dict):
            return []
        included = raw.get("included")
        if not isinstance(included, list):
            return []
        out: list[dict[str, Any]] = []
        for item in included:
            if not isinstance(item, dict) or item.get("type") != "OwnBases":
                continue
            attrs = item.get("attributes")
            if not isinstance(attrs, dict):
                continue
            entry = dict(attrs)
            if (item_id := item.get("id")) and "uuid" not in entry:
                entry["uuid"] = item_id
            out.append(entry)
        return out

    async def get_device_extended(self, uuid: str) -> dict[str, Any]:
        """GET /devices/{uuid} – richer per-device record with `included[]`.

        Undocumented; only used to surface the friendly model name (e.g.
        "A 15v") via `included[].DeviceDetails.attributes.soap_info.item[0].Name`.
        Returns the raw response or `{}` on failure.
        """
        try:
            return await self._get(EP_DEVICE.format(uuid=uuid)) or {}
        except StigaApiError as err:
            _LOGGER.debug("/devices/%s unavailable: %s", uuid, err)
            return {}

    async def get_perimeter(self, uuid: str, base_uuid: str) -> dict[str, Any]:
        """GET /perimeters?device_uuid=&base_uuid= – garden perimeter summary.

        Both query params are mandatory. Undocumented. Returns the raw
        response or `{}` on failure.
        """
        try:
            return await self._get(EP_PERIMETER.format(uuid=uuid, base_uuid=base_uuid)) or {}
        except StigaApiError as err:
            _LOGGER.debug("/perimeters for %s unavailable: %s", uuid, err)
            return {}

    # ------------------------------------------------------------------ Status

    async def get_device_status(self, uuid: str) -> dict[str, Any]:
        """GET /devices/{uuid}/mqttstatus – fetch and parse raw status.

        NOTE: this endpoint is NOT part of the official STIGA Integration API
        documentation (which only covers /garage/integration and the
        startsession/endsession commands). It is used by the STIGA.GO app
        itself and may change without notice.
        """
        raw = await self._get(EP_STATUS.format(uuid=uuid))
        return self._parse_status(raw)

    @staticmethod
    def _load_json_field(val: Any) -> dict[str, Any]:
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
            except json.JSONDecodeError:
                _LOGGER.warning("Failed to parse JSON field: %.120r", val)
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return val if isinstance(val, dict) else {}

    def _parse_status(self, raw: dict[str, Any]) -> dict[str, Any]:
        """
        Parse the mqttstatus response.
        Known structure (vista_robot):
          raw["data"]["attributes"]["device_info"]
            "status":  { "description": JSON string }
            "battery": { "description": JSON string }
        """
        # Structure 1: data.attributes.device_info (vista_robot, autonomous_robot)
        try:
            info = raw["data"]["attributes"]["device_info"]
            status = self._load_json_field(info["status"]["description"])
            batt = self._load_json_field(info["battery"]["description"])
            return self._build_status(status, batt)
        except (KeyError, TypeError):
            pass

        # Structure 2: flat at root
        if "mowingMode" in raw or "currentAction" in raw:
            return self._build_status(raw, raw.get("battery") or {})

        # Structure 3: under 'attributes'
        attrs = raw.get("attributes") or {}
        if "mowingMode" in attrs:
            return self._build_status(attrs, attrs.get("battery") or {})

        _LOGGER.warning("Unknown status structure: %s", list(raw.keys()))
        return {}

    @staticmethod
    def _build_status(s: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        """Build a flat status dict from raw API data."""
        ca = s.get("currentAction")
        mm = s.get("mowingMode")
        has_data = s.get("hasData")
        pct = b.get("percentage")
        voltage = b.get("voltage")
        cap = b.get("capacity")
        rem = b.get("remainingCapacity")
        cycles = b.get("numberOfCycles")
        t_left = b.get("dischargingTime")
        current = b.get("current")
        charging = b.get("charging")

        power_w = None
        if voltage is not None and current is not None:
            power_w = round(abs(current) * voltage, 2)

        health = None
        # `cap` must be truthy to avoid division by zero; `rem` may legitimately
        # be 0 (empty pack), so guard it with `is not None`, not truthiness.
        if cap and rem is not None:
            health = round((rem / cap) * 100, 1)

        # Fields already represented as first-class attributes – don't echo them
        # back into `extra`. `battery` is excluded because it's a sub-dict and
        # rendering `extra_battery: {...}` on the entity isn't useful.
        _consumed = {"mowingMode", "currentAction", "errorCode", "isDocked", "hasData", "battery"}

        return {
            "has_data": has_data,
            "mowing_mode": mm,
            "current_action": ca,
            "is_docked": s.get("isDocked"),
            "error_code": s.get("errorCode"),
            # Batterie
            "battery_level": pct,
            "battery_charging": charging,
            "battery_voltage": round(voltage, 3) if voltage is not None else None,
            "battery_capacity": cap,
            "battery_remaining": rem,
            "battery_cycles": cycles,
            "battery_time_left": t_left,
            "battery_current": round(current, 4) if current is not None else None,
            "battery_power_w": power_w,
            "battery_health": health,
            # Additional raw fields not yet mapped above
            "extra": {k: v for k, v in s.items() if k not in _consumed},
        }

    # ------------------------------------------------------------------ Commands

    async def start_mowing(self, uuid: str, zone_id: int | None = None) -> None:
        """POST /devices/{uuid}/command/startsession"""
        body = {"data": {"zone_id": zone_id}} if zone_id is not None else None
        await self._post(EP_START.format(uuid=uuid), body=body)

    async def stop_mowing(self, uuid: str) -> None:
        """POST /devices/{uuid}/command/endsession"""
        await self._post(EP_STOP.format(uuid=uuid))

    # ------------------------------------------------------------------ Connection test

    async def test_connection(self) -> bool:
        """Connection test for the config flow."""
        await self.authenticate()
        devices = await self.get_devices()
        return len(devices) > 0
