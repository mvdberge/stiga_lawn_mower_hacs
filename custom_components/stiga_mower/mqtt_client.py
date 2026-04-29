"""Async MQTT client for the STIGA cloud broker.

Wraps :mod:`aiomqtt` with mTLS authentication, topic-based dispatch, and
preemptive token refresh. Frame parsing lives in :mod:`mqtt_messages`; this
module is purely the transport.

Topic conventions (mirrored from matthewgream/stiga-api):

  * Robot subscribes:  ``{mac}/LOG/+``,  ``{mac}/JSON_NOTIFICATION``,
                       ``CMD_ROBOT_ACK/{mac}``
  * Robot publishes:   ``{mac}/CMD_ROBOT``  (QoS 2)
  * Base subscribes:   ``{base_mac}/LOG/+``, ``{base_mac}/JSON_NOTIFICATION``,
                       ``CMD_REFERENCE_ACK/{base_mac}``
  * Base publishes:    ``{base_mac}/CMD_REFERENCE``  (QoS 2)

The same MQTT connection serves every device on a given account, since the
broker authenticates by Firebase id-token rather than per-device. We keep a
single :class:`aiomqtt.Client` and tear it down once an hour to refresh the
token before it expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiomqtt
from homeassistant.core import HomeAssistant

from . import mqtt_constants as mc
from . import mqtt_messages as mm

_LOGGER = logging.getLogger(__name__)

_CERT_DIR = Path(__file__).parent / "certs"


# Callback signatures — kept loose so handlers can be plain sync callables
# (the dispatch hub never awaits them; coordinator does its own scheduling).
StatusCallback = Callable[[str, dict[str, Any]], None]
ConnectionCallback = Callable[[bool], None]


class StigaMQTTError(Exception):
    """Raised when an MQTT operation fails (e.g. publish before connect)."""


class StigaMQTT:
    """Single-connection STIGA MQTT client."""

    def __init__(
        self,
        hass: HomeAssistant,
        token_provider: Callable[[], Awaitable[str]],
        *,
        broker_id: str | None = None,
        client_id: str | None = None,
        cert_path: Path | None = None,
        key_path: Path | None = None,
    ) -> None:
        self._hass = hass
        self._token_provider = token_provider
        self._broker_id = broker_id or mc.MQTT_BROKER_HOST_FALLBACK
        self._client_id = client_id or f"hass_stiga_{uuid.uuid4().hex[:12]}"
        self._cert_path = cert_path or (_CERT_DIR / mc.MQTT_CERT_FILE)
        self._key_path = key_path or (_CERT_DIR / mc.MQTT_KEY_FILE)

        # Registered devices. Values currently unused but reserved for
        # per-device metadata (e.g. friendly name, base linkage).
        self._robots: dict[str, dict[str, Any]] = {}
        self._bases: dict[str, dict[str, Any]] = {}

        # Handlers
        self._on_status: StatusCallback | None = None
        self._on_position: StatusCallback | None = None
        self._on_settings: StatusCallback | None = None
        self._on_schedule: StatusCallback | None = None
        self._on_base_status: StatusCallback | None = None
        self._on_notification: StatusCallback | None = None
        self._on_command_ack: StatusCallback | None = None
        self._on_connection_change: ConnectionCallback | None = None

        # Runtime
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._client: aiomqtt.Client | None = None
        self._connected = False

    # -------------------------------------------------------------- Setup

    def add_robot(self, mac: str, **meta: Any) -> None:
        self._robots[mac] = meta

    def add_base(self, mac: str, **meta: Any) -> None:
        self._bases[mac] = meta

    def set_handlers(
        self,
        *,
        on_status: StatusCallback | None = None,
        on_position: StatusCallback | None = None,
        on_settings: StatusCallback | None = None,
        on_schedule: StatusCallback | None = None,
        on_base_status: StatusCallback | None = None,
        on_notification: StatusCallback | None = None,
        on_command_ack: StatusCallback | None = None,
        on_connection_change: ConnectionCallback | None = None,
    ) -> None:
        if on_status is not None:
            self._on_status = on_status
        if on_position is not None:
            self._on_position = on_position
        if on_settings is not None:
            self._on_settings = on_settings
        if on_schedule is not None:
            self._on_schedule = on_schedule
        if on_base_status is not None:
            self._on_base_status = on_base_status
        if on_notification is not None:
            self._on_notification = on_notification
        if on_command_ack is not None:
            self._on_command_ack = on_command_ack
        if on_connection_change is not None:
            self._on_connection_change = on_connection_change

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def broker_host(self) -> str:
        return mc.MQTT_BROKER_HOST_TEMPLATE.format(broker_id=self._broker_id)

    # -------------------------------------------------------------- Lifecycle

    async def start(self) -> None:
        """Spawn the background connect/reconnect task."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = self._hass.async_create_background_task(self._run_loop(), name="stiga_mqtt")

    async def stop(self) -> None:
        """Stop the connection loop and wait for it to exit."""
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # -------------------------------------------------------------- Run loop

    async def _run_loop(self) -> None:
        """Outer reconnect loop; one iteration = one full session."""
        while not self._stop_event.is_set():
            try:
                await self._connect_session()
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as err:
                _LOGGER.warning(
                    "MQTT connection lost: %s — reconnecting in %ds",
                    err,
                    mc.MQTT_RECONNECT_DELAY,
                )
            except Exception:
                _LOGGER.exception("Unexpected MQTT loop error")
            finally:
                self._set_connected(False)
            if self._stop_event.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=mc.MQTT_RECONNECT_DELAY,
                )

    async def _connect_session(self) -> None:
        """One connect/subscribe/dispatch cycle, broken by token refresh."""
        ssl_ctx = await self._hass.async_add_executor_job(self._build_ssl)
        token = await self._token_provider()

        async with aiomqtt.Client(
            hostname=self.broker_host,
            port=mc.MQTT_BROKER_PORT,
            username=mc.MQTT_BROKER_USERNAME,
            password=token,
            tls_context=ssl_ctx,
            identifier=self._client_id,
            keepalive=mc.MQTT_KEEPALIVE,
        ) as client:
            self._client = client
            self._set_connected(True)
            poll_task: asyncio.Task[None] | None = None
            try:
                for topic in self._subscriptions():
                    await client.subscribe(topic, qos=0)
                    _LOGGER.debug("Subscribed: %s", topic)

                # STIGA robots do not push status frames — they must be polled.
                # Send an immediate request, then keep a background task polling
                # every MQTT_STATUS_POLL_INTERVAL seconds for the duration of
                # this MQTT session.
                await self._poll_all_robots()
                poll_task = asyncio.create_task(self._poll_loop(), name="stiga_mqtt_poll")

                # Race the message consumer against the refresh timer.
                # On timeout we cleanly close the session so the outer loop
                # reconnects with a fresh Firebase token.
                try:
                    async with asyncio.timeout(mc.MQTT_TOKEN_REFRESH_INTERVAL):
                        async for message in client.messages:
                            if self._stop_event.is_set():
                                return
                            self._dispatch(str(message.topic), bytes(message.payload))
                except TimeoutError:
                    _LOGGER.debug("Token refresh due — cycling MQTT connection")
            finally:
                if poll_task is not None:
                    poll_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await poll_task
                self._client = None

    async def _poll_loop(self) -> None:
        """Periodically request status from all robots while connected."""
        while not self._stop_event.is_set() and self._connected:
            try:
                await asyncio.sleep(mc.MQTT_STATUS_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise
            if self._stop_event.is_set() or not self._connected:
                return
            await self._poll_all_robots()

    async def _poll_all_robots(self) -> None:
        """Send a STATUS_REQUEST to every registered robot."""
        for mac in list(self._robots):
            try:
                await self.request_status(mac)
                _LOGGER.debug("Polled status from robot %s", mac)
            except Exception as err:
                _LOGGER.warning("Failed to request status from %s: %s", mac, err)

    def _subscriptions(self) -> list[str]:
        topics: list[str] = []
        for mac in self._robots:
            topics.append(mc.ROBOT_TOPIC_LOG_WILDCARD.format(mac=mac))
            topics.append(mc.ROBOT_TOPIC_NOTIFICATION.format(mac=mac))
            topics.append(mc.ROBOT_TOPIC_CMD_ACK.format(mac=mac))
        for mac in self._bases:
            topics.append(mc.BASE_TOPIC_LOG_WILDCARD.format(mac=mac))
            topics.append(mc.BASE_TOPIC_NOTIFICATION.format(mac=mac))
            topics.append(mc.BASE_TOPIC_CMD_ACK.format(mac=mac))
        return topics

    def _build_ssl(self) -> ssl.SSLContext:
        """Build the mTLS context. Runs on the executor — sync I/O."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_cert_chain(certfile=self._cert_path, keyfile=self._key_path)
        # Match matthewgream's `rejectUnauthorized: false`. STIGA's broker
        # presents a self-signed cert (not chained to any public root) so
        # we cannot validate it; refusing the connection would simply
        # break the integration.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _set_connected(self, value: bool) -> None:
        if self._connected == value:
            return
        self._connected = value
        if self._on_connection_change is not None:
            with contextlib.suppress(Exception):
                self._on_connection_change(value)

    # -------------------------------------------------------------- Dispatch

    def _dispatch(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) == 3 and parts[1] == "LOG":
            self._dispatch_log(parts[0], parts[2], payload)
        elif len(parts) == 2 and parts[1] == "JSON_NOTIFICATION":
            self._fire(self._on_notification, parts[0], mm.decode_notification(payload))
        elif (len(parts) == 2 and parts[0] == "CMD_ROBOT_ACK") or (
            len(parts) == 2 and parts[0] == "CMD_REFERENCE_ACK"
        ):
            self._fire(self._on_command_ack, parts[1], mm.decode_command_ack(payload))
        else:
            _LOGGER.debug("Ignoring unhandled topic: %s", topic)

    def _dispatch_log(self, mac: str, kind: str, payload: bytes) -> None:
        if mac in self._robots:
            self._dispatch_robot_log(mac, kind, payload)
        elif mac in self._bases:
            self._dispatch_base_log(mac, kind, payload)
        else:
            _LOGGER.debug("LOG topic for unregistered MAC %s (kind=%s)", mac, kind)

    def _dispatch_robot_log(self, mac: str, kind: str, payload: bytes) -> None:
        if kind == mc.ROBOT_LOG_STATUS:
            if not self._robots.get(mac):
                _LOGGER.warning("STATUS frame for unregistered robot MAC %s — check _build_mqtt()", mac)
            self._fire(self._on_status, mac, mm.decode_status(payload))
        elif kind == mc.ROBOT_LOG_POSITION:
            self._fire(self._on_position, mac, mm.decode_position(payload))
        elif kind == mc.ROBOT_LOG_SETTINGS:
            self._fire(self._on_settings, mac, mm.decode_settings(payload))
        elif kind == mc.ROBOT_LOG_SCHEDULING:
            self._fire(self._on_schedule, mac, mm.decode_schedule(payload))
        elif kind == mc.ROBOT_LOG_VERSION:
            _LOGGER.debug("Robot %s VERSION frame ignored (Phase 4 will surface it)", mac)
        else:
            _LOGGER.debug("Robot %s sent unknown LOG kind: %s", mac, kind)

    def _dispatch_base_log(self, mac: str, kind: str, payload: bytes) -> None:
        if kind == mc.BASE_LOG_STATUS:
            self._fire(self._on_base_status, mac, mm.decode_base_status(payload))
        elif kind == mc.BASE_LOG_VERSION:
            _LOGGER.debug("Base %s VERSION frame ignored", mac)
        else:
            _LOGGER.debug("Base %s sent unknown LOG kind: %s", mac, kind)

    @staticmethod
    def _fire(
        handler: StatusCallback | None,
        mac: str,
        payload: dict[str, Any],
    ) -> None:
        if handler is None:
            return
        try:
            handler(mac, payload)
        except Exception:
            _LOGGER.exception("Handler raised for %s", mac)

    # -------------------------------------------------------------- Publish

    async def _publish(self, topic: str, payload: bytes, *, qos: int = 2) -> None:
        if self._client is None or not self._connected:
            raise StigaMQTTError("MQTT not connected — cannot publish")
        await self._client.publish(topic, payload=payload, qos=qos)

    async def request_status(self, mac: str, **flags: bool) -> None:
        """Ask the mower to emit a STATUS frame.

        Without args every sub-frame (battery + mowing + location + network)
        is requested; pass keyword flags to scope (e.g. ``battery=True,
        location=False``).
        """
        payload = mm.encode_status_request(**flags) if flags else mm.encode_status_request()
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def request_position(self, mac: str) -> None:
        payload = mm.encode_simple_request(mc.ROBOT_CMD_POSITION_REQUEST)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def request_settings(self, mac: str) -> None:
        payload = mm.encode_simple_request(mc.ROBOT_CMD_SETTINGS_REQUEST)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def request_schedule(self, mac: str) -> None:
        payload = mm.encode_simple_request(mc.ROBOT_CMD_SCHEDULING_SETTINGS_REQUEST)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def cmd_start(self, mac: str) -> None:
        """Send ROBOT_CMD_START (1) — begin a mowing session."""
        payload = mm.encode_command(mc.ROBOT_CMD_START)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def cmd_stop(self, mac: str) -> None:
        """Send ROBOT_CMD_STOP (0) — pause in place (REST endsession goes home)."""
        payload = mm.encode_command(mc.ROBOT_CMD_STOP)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def cmd_go_home(self, mac: str) -> None:
        """Send ROBOT_CMD_GO_HOME (4) — return to dock."""
        payload = mm.encode_command(mc.ROBOT_CMD_GO_HOME)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def cmd_calibrate_blades(self, mac: str) -> None:
        """Send ROBOT_CMD_CALIBRATE_BLADES (26)."""
        payload = mm.encode_simple_request(mc.ROBOT_CMD_CALIBRATE_BLADES)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def cmd_settings_update(self, mac: str, settings: dict) -> None:
        """Send ROBOT_CMD_SETTINGS_UPDATE (18) with the given settings fields."""
        payload = mm.encode_settings_update(settings)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def cmd_schedule_update(self, mac: str, blob: bytes) -> None:
        """Send ROBOT_CMD_SCHEDULING_SETTINGS_UPDATE (20) with the packed schedule blob."""
        payload = mm.encode_command(mc.ROBOT_CMD_SCHEDULING_SETTINGS_UPDATE, {2: blob})
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)
