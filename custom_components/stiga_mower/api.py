"""Asynchroner Client für die STIGA Integration API."""

from __future__ import annotations

import json
import logging

import aiohttp

from .const import (
    FIREBASE_API_KEY,
    FIREBASE_AUTH_URL,
    STIGA_BASE_URL,
    EP_GARAGE,
    EP_STATUS,
    EP_START,
    EP_STOP,
)

_LOGGER = logging.getLogger(__name__)


class StigaAuthError(Exception):
    """Authentifizierungsfehler."""


class StigaApiError(Exception):
    """Allgemeiner API-Fehler."""


class StigaAPI:
    """
    Asynchroner REST-Client für die STIGA Integration API.

    Authentifizierung über Firebase verifyPassword (idToken).
    Alle STIGA-Endpunkte werden mit Bearer-Token autorisiert.
    """

    def __init__(self, email: str, password: str, session: aiohttp.ClientSession) -> None:
        self._email    = email
        self._password = password
        self._session  = session
        self._token: str | None = None

    # ------------------------------------------------------------------ Auth

    async def authenticate(self) -> None:
        """Firebase-Login → speichert idToken intern."""
        try:
            async with self._session.post(
                FIREBASE_AUTH_URL,
                json={
                    "email":             self._email,
                    "password":          self._password,
                    "returnSecureToken": True,
                },
                params={"key": FIREBASE_API_KEY},
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    msg = data.get("error", {}).get("message", str(resp.status))
                    raise StigaAuthError(f"Authentifizierung fehlgeschlagen: {msg}")
                self._token = data["idToken"]
                _LOGGER.debug("Firebase-Authentifizierung erfolgreich.")
        except aiohttp.ClientError as err:
            raise StigaApiError(f"Netzwerkfehler bei Authentifizierung: {err}") from err

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def _get(self, path: str, retry: bool = True):
        if not self._token:
            await self.authenticate()
        try:
            async with self._session.get(
                f"{STIGA_BASE_URL}{path}",
                headers=self._auth_header(),
            ) as resp:
                if resp.status == 401 and retry:
                    _LOGGER.debug("Token abgelaufen – erneute Anmeldung.")
                    await self.authenticate()
                    return await self._get(path, retry=False)
                if resp.status != 200:
                    raise StigaApiError(f"GET {path} → HTTP {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise StigaApiError(f"Netzwerkfehler: {err}") from err

    async def _post(self, path: str, body=None, retry: bool = True):
        if not self._token:
            await self.authenticate()
        try:
            async with self._session.post(
                f"{STIGA_BASE_URL}{path}",
                headers=self._auth_header(),
                json=body,
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
            raise StigaApiError(f"Netzwerkfehler: {err}") from err

    # ------------------------------------------------------------------ Geräte

    async def get_devices(self) -> list[dict]:
        """
        GET /garage/integration
        Laut offizieller Doku: { "Data": [ { "type": ..., "attributes": { uuid, name, ... } } ] }
        """
        raw = await self._get(EP_GARAGE)
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
        """GET /devices/{uuid}/mqttstatus – Rohstatus abrufen und parsen."""
        raw = await self._get(EP_STATUS.format(uuid=uuid))
        return self._parse_status(raw)

    @staticmethod
    def _load_json_field(val) -> dict:
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return {}
        return val or {}

    def _parse_status(self, raw: dict) -> dict:
        """
        Wertet den mqttstatus aus.
        Bekannte Struktur (vista_robot):
          raw["data"]["attributes"]["device_info"]
            "status":  { "description": JSON-String }
            "battery": { "description": JSON-String }
        """
        # Struktur 1: data.attributes.device_info (vista_robot, autonomous_robot)
        try:
            info   = raw["data"]["attributes"]["device_info"]
            status = self._load_json_field(info["status"]["description"])
            batt   = self._load_json_field(info["battery"]["description"])
            return self._build_status(status, batt)
        except (KeyError, TypeError):
            pass

        # Struktur 2: flach im Root
        if "mowingMode" in raw or "currentAction" in raw:
            return self._build_status(raw, raw.get("battery") or {})

        # Struktur 3: unter 'attributes'
        attrs = raw.get("attributes") or {}
        if "mowingMode" in attrs:
            return self._build_status(attrs, attrs.get("battery") or {})

        _LOGGER.warning("Unbekannte Status-Struktur: %s", list(raw.keys()))
        return {}

    @staticmethod
    def _build_status(s: dict, b: dict) -> dict:
        """Flaches Status-Dict aus API-Rohdaten aufbauen."""
        ca      = s.get("currentAction")
        mm      = s.get("mowingMode") or ca
        pct     = b.get("percentage")
        voltage = b.get("voltage")
        cap     = b.get("capacity")
        rem     = b.get("remainingCapacity")
        cycles  = b.get("numberOfCycles")
        t_left  = b.get("dischargingTime")
        current = b.get("current")
        charging = b.get("charging")

        power_w = None
        if voltage is not None and current is not None:
            power_w = round(abs(current) * voltage, 2)

        health = None
        if cap and rem:
            health = round((rem / cap) * 100, 1)

        return {
            "mowing_mode":       mm,
            "current_action":    ca,
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
            # Weitere Rohfelder
            "extra": {
                k: v for k, v in s.items()
                if k not in ("mowingMode", "currentAction", "errorCode")
            },
        }

    # ------------------------------------------------------------------ Befehle

    async def start_mowing(self, uuid: str, zone_id: int | None = None) -> None:
        """POST /devices/{uuid}/command/startsession"""
        body = {"data": {"zone_id": zone_id}} if zone_id is not None else None
        await self._post(EP_START.format(uuid=uuid), body=body)

    async def stop_mowing(self, uuid: str) -> None:
        """POST /devices/{uuid}/command/endsession"""
        await self._post(EP_STOP.format(uuid=uuid))

    # ------------------------------------------------------------------ Test

    async def test_connection(self) -> bool:
        """Verbindungstest für den Config Flow."""
        await self.authenticate()
        devices = await self.get_devices()
        return len(devices) > 0
