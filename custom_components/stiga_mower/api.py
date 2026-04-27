"""Async client for the STIGA Integration API."""

from __future__ import annotations

import json
import logging

import aiohttp

from .const import (
    FIREBASE_API_KEY,
    FIREBASE_AUTH_URL,
    STIGA_BASE_URL,
    EP_GARAGE,
    EP_GARAGE_FULL,
    EP_STATUS,
    EP_START,
    EP_STOP,
    REQUEST_TIMEOUT,
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
        self._email    = email
        self._password = password
        self._session  = session
        self._token: str | None = None

    # ------------------------------------------------------------------ Auth

    async def authenticate(self) -> None:
        """Firebase login – stores idToken internally."""
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        try:
            async with self._session.post(
                FIREBASE_AUTH_URL,
                json={
                    "email":             self._email,
                    "password":          self._password,
                    "returnSecureToken": True,
                },
                params={"key": FIREBASE_API_KEY},
                timeout=timeout,
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    msg = data.get("error", {}).get("message", str(resp.status))
                    raise StigaAuthError(f"Authentication failed: {msg}")
                self._token = data["idToken"]
                _LOGGER.debug("Firebase authentication successful.")
        except aiohttp.ClientError as err:
            raise StigaApiError(f"Network error during authentication: {err}") from err

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def _get(self, path: str, retry: bool = True):
        if not self._token:
            await self.authenticate()
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
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
                if resp.status != 200:
                    raise StigaApiError(f"GET {path} → HTTP {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise StigaApiError(f"Network error: {err}") from err

    async def _post(self, path: str, body=None, retry: bool = True):
        if not self._token:
            await self.authenticate()
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
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
                if resp.status not in (200, 204):
                    raise StigaApiError(f"POST {path} → HTTP {resp.status}")
                try:
                    return await resp.json() if resp.content_length else None
                except Exception:
                    return None
        except aiohttp.ClientError as err:
            raise StigaApiError(f"Network error: {err}") from err

    # ------------------------------------------------------------------ Devices

    async def get_devices(self) -> list[dict]:
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
    def _extract_devices(raw) -> list[dict]:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("Data", "data", "devices", "robots", "items"):
                if isinstance(raw.get(key), list):
                    return raw[key]
            return [raw]
        return []

    # ------------------------------------------------------------------ Status

    async def get_device_status(self, uuid: str) -> dict:
        """GET /devices/{uuid}/mqttstatus – fetch and parse raw status.

        NOTE: this endpoint is NOT part of the official STIGA Integration API
        documentation (which only covers /garage/integration and the
        startsession/endsession commands). It is used by the STIGA.GO app
        itself and may change without notice.
        """
        raw = await self._get(EP_STATUS.format(uuid=uuid))
        return self._parse_status(raw)

    @staticmethod
    def _load_json_field(val) -> dict:
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                _LOGGER.warning("Failed to parse JSON field: %.120r", val)
                return {}
        return val or {}

    def _parse_status(self, raw: dict) -> dict:
        """
        Parse the mqttstatus response.
        Known structure (vista_robot):
          raw["data"]["attributes"]["device_info"]
            "status":  { "description": JSON string }
            "battery": { "description": JSON string }
        """
        # Structure 1: data.attributes.device_info (vista_robot, autonomous_robot)
        try:
            info   = raw["data"]["attributes"]["device_info"]
            status = self._load_json_field(info["status"]["description"])
            batt   = self._load_json_field(info["battery"]["description"])
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
    def _build_status(s: dict, b: dict) -> dict:
        """Build a flat status dict from raw API data."""
        ca       = s.get("currentAction")
        mm       = s.get("mowingMode")
        has_data = s.get("hasData")
        pct      = b.get("percentage")
        voltage  = b.get("voltage")
        cap      = b.get("capacity")
        rem      = b.get("remainingCapacity")
        cycles   = b.get("numberOfCycles")
        t_left   = b.get("dischargingTime")
        current  = b.get("current")
        charging = b.get("charging")

        power_w = None
        if voltage is not None and current is not None:
            power_w = round(abs(current) * voltage, 2)

        health = None
        if cap and rem:
            health = round((rem / cap) * 100, 1)

        # Fields already represented as first-class attributes – don't echo them
        # back into `extra`. `battery` is excluded because it's a sub-dict and
        # rendering `extra_battery: {...}` on the entity isn't useful.
        _consumed = {"mowingMode", "currentAction", "errorCode", "isDocked",
                     "hasData", "battery"}

        return {
            "has_data":          has_data,
            "mowing_mode":       mm,
            "current_action":    ca,
            "is_docked":         s.get("isDocked"),
            "error_code":        s.get("errorCode"),
            # Batterie
            "battery_level":     pct,
            "battery_charging":  charging,
            "battery_voltage":   round(voltage, 3) if voltage else None,
            "battery_capacity":  cap,
            "battery_remaining": rem,
            "battery_cycles":    cycles,
            "battery_time_left": t_left,
            "battery_current":   round(current, 4) if current is not None else None,
            "battery_power_w":   power_w,
            "battery_health":    health,
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
