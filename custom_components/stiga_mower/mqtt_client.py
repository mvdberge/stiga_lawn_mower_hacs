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
import time
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
FailureCallback = Callable[[str], None]

# Consecutive failed connect attempts before we surface a repair issue. start()
# is non-blocking, so a permanently-unreachable broker can only be observed from
# inside the reconnect loop; a few retries first avoid flapping on brief blips.
MQTT_CONNECT_FAILURES_BEFORE_REPAIR = 3


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
        self._on_settings: StatusCallback | None = None
        self._on_schedule: StatusCallback | None = None
        self._on_base_status: StatusCallback | None = None
        self._on_base_version: StatusCallback | None = None
        self._on_notification: StatusCallback | None = None
        self._on_command_ack: StatusCallback | None = None
        self._on_connection_change: ConnectionCallback | None = None
        self._on_connect_failed: FailureCallback | None = None

        # Runtime
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._client: aiomqtt.Client | None = None
        self._connected = False
        # Consecutive failed connect attempts; reset whenever a session connects.
        self._consecutive_connect_failures = 0
        # Monotonic timestamp of the moment the current session became
        # connected, or None while no session is up. Used by `_run_loop` to
        # tell a long-lived-then-dropped session apart from a reconnect storm.
        self._connected_since: float | None = None

    # -------------------------------------------------------------- Setup

    def add_robot(self, mac: str, **meta: Any) -> None:
        self._robots[mac] = meta

    def add_base(self, mac: str, **meta: Any) -> None:
        self._bases[mac] = meta

    def set_handlers(
        self,
        *,
        on_status: StatusCallback | None = None,
        on_settings: StatusCallback | None = None,
        on_schedule: StatusCallback | None = None,
        on_base_status: StatusCallback | None = None,
        on_base_version: StatusCallback | None = None,
        on_notification: StatusCallback | None = None,
        on_command_ack: StatusCallback | None = None,
        on_connection_change: ConnectionCallback | None = None,
        on_connect_failed: FailureCallback | None = None,
    ) -> None:
        if on_status is not None:
            self._on_status = on_status
        if on_settings is not None:
            self._on_settings = on_settings
        if on_schedule is not None:
            self._on_schedule = on_schedule
        if on_base_status is not None:
            self._on_base_status = on_base_status
        if on_base_version is not None:
            self._on_base_version = on_base_version
        if on_notification is not None:
            self._on_notification = on_notification
        if on_command_ack is not None:
            self._on_command_ack = on_command_ack
        if on_connection_change is not None:
            self._on_connection_change = on_connection_change
        if on_connect_failed is not None:
            self._on_connect_failed = on_connect_failed

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
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # The run loop may exit via `break` right after a clean token refresh,
        # where the finally block deliberately skips the disconnect. Announce
        # it unconditionally here (idempotent when already False) so
        # `mqtt_connected` never stays True after the client has stopped.
        self._set_connected(False)

    # -------------------------------------------------------------- Run loop

    async def _run_loop(self) -> None:
        """Outer reconnect loop; one iteration = one full session.

        Uses exponential backoff on consecutive failures so a persistent broker
        outage does not result in a tight reconnect storm. Delay resets to the
        base value after a successful session — either a clean token-refresh
        cycle or any session that stayed connected long enough to be considered
        healthy before dropping.
        """
        delay = mc.MQTT_RECONNECT_DELAY
        # A session that stayed connected at least this long is treated as
        # healthy: its drop is a fresh fault rather than a continuation of a
        # reconnect storm, so the backoff resets to the base delay even when
        # the session ended via an exception (e.g. broker drops after 40 min).
        long_session_s = max(60, mc.MQTT_RECONNECT_DELAY * 2)
        while not self._stop_event.is_set():
            clean_refresh = False
            self._connected_since = None
            try:
                clean_refresh = await self._connect_session()
                # Session ended cleanly (token refresh) — reset backoff.
                delay = mc.MQTT_RECONNECT_DELAY
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as err:
                _LOGGER.warning(
                    "MQTT connection lost: %s — reconnecting in %ds",
                    err,
                    delay,
                )
                self._note_connect_failure(str(err))
            except Exception as err:
                _LOGGER.exception("Unexpected MQTT loop error")
                self._note_connect_failure(str(err))
            finally:
                # A session that stayed up long enough before dropping is not
                # part of a reconnect storm — reset the backoff so a broker
                # that connects fine but drops after a while does not keep
                # doubling the delay toward MQTT_RECONNECT_DELAY_MAX.
                if (
                    self._connected_since is not None
                    and time.monotonic() - self._connected_since >= long_session_s
                ):
                    delay = mc.MQTT_RECONNECT_DELAY
                # Only announce a disconnect for *unplanned* drops. A planned
                # token-refresh cycle reconnects immediately in the next loop
                # iteration, so flipping `mqtt_connected` to False here would
                # publish a spurious False→True transition every ~50 minutes
                # and flap the availability of every MQTT-derived entity. We
                # keep `_connected` True across the refresh and only fall back
                # to False if the immediate reconnect itself fails (that next
                # iteration starts with clean_refresh=False, so the drop is
                # announced then).
                if not clean_refresh:
                    self._set_connected(False)
            if self._stop_event.is_set():
                break
            if clean_refresh:
                # Planned token-refresh cycle: reconnect immediately so the
                # connection gap stays short. `_connected` intentionally stays
                # True across this gap (see the finally above) to avoid a
                # visible availability flap every ~50 minutes.
                continue
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=delay,
                )
            delay = min(delay * 2, mc.MQTT_RECONNECT_DELAY_MAX)

    async def _connect_session(self) -> bool:
        """One connect/subscribe/dispatch cycle, broken by token refresh.

        Returns True when the session ended because the token-refresh timer
        fired — a planned, healthy cycle — so the caller can reconnect
        immediately instead of applying reconnect backoff.
        """
        ssl_ctx = await self._hass.async_add_executor_job(self._build_ssl)
        token = await self._token_provider()
        refreshed = False

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
            # A successful connect clears the failure streak that drives the
            # repair issue.
            self._consecutive_connect_failures = 0
            # Record when this session became connected so `_run_loop` can
            # reset the reconnect backoff after a long-lived session drops.
            self._connected_since = time.monotonic()
            poll_task: asyncio.Task[None] | None = None
            try:
                for topic in self._subscriptions():
                    # LOG topics carry the polled SETTINGS/SCHEDULING/STATUS
                    # response frames; those are never re-requested on loss, so
                    # subscribe at QoS 1 to have the broker redeliver a dropped
                    # frame. Command-ack and notification topics stay at QoS 0.
                    qos = 1 if "/LOG/" in topic else 0
                    await client.subscribe(topic, qos=qos)
                    _LOGGER.debug("Subscribed: %s (qos=%d)", topic, qos)

                # STIGA robots do not push status frames — they must be polled.
                # Send an immediate request, then keep a background task polling
                # every MQTT_STATUS_POLL_INTERVAL seconds for the duration of
                # this MQTT session.
                await self._poll_all_robots()
                # Settings and schedule are not pushed spontaneously either.
                # Request both once at connection time so switch/select/calendar
                # entities populate immediately.  Subsequent writes via
                # cmd_settings_update / cmd_schedule_set_enabled will trigger
                # a new frame automatically.
                await self._request_all_settings()
                await self._request_all_schedule()
                poll_task = asyncio.create_task(self._poll_loop(), name="stiga_mqtt_poll")

                # Race the message consumer against the refresh timer.
                # On timeout we cleanly close the session so the outer loop
                # reconnects with a fresh Firebase token.
                try:
                    async with asyncio.timeout(mc.MQTT_TOKEN_REFRESH_INTERVAL):
                        async for message in client.messages:
                            if self._stop_event.is_set():
                                return False
                            self._dispatch(str(message.topic), bytes(message.payload))
                except TimeoutError:
                    _LOGGER.debug("Token refresh due — cycling MQTT connection")
                    refreshed = True
            finally:
                if poll_task is not None:
                    poll_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await poll_task
                self._client = None
        return refreshed

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

    async def _request_all_settings(self) -> None:
        """Send a SETTINGS_REQUEST to every registered robot (once at connection)."""
        for mac in list(self._robots):
            try:
                await self.request_settings(mac)
                _LOGGER.debug("Requested settings from robot %s", mac)
            except Exception as err:
                _LOGGER.warning("Failed to request settings from %s: %s", mac, err)

    async def _request_all_schedule(self) -> None:
        """Send a SCHEDULING_SETTINGS_REQUEST to every registered robot (once at connection)."""
        for mac in list(self._robots):
            try:
                await self.request_schedule(mac)
                _LOGGER.debug("Requested schedule from robot %s", mac)
            except Exception as err:
                _LOGGER.warning("Failed to request schedule from %s: %s", mac, err)

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
        # KNOWN LIMITATION: server-certificate verification is disabled. STIGA's
        # broker presents a self-signed certificate that is not chained to any
        # public root and is not published anywhere we can pin against, so
        # enabling CERT_REQUIRED would break the connection for every user. The
        # connection is still mutually authenticated by the client certificate
        # loaded above; the residual exposure is an on-path MITM, documented in
        # the README. Revisit if STIGA ever publishes a pinnable broker cert.
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

    def _note_connect_failure(self, error: str) -> None:
        """Count a failed connect cycle and surface a repair issue once the
        broker has been unreachable for several attempts in a row."""
        self._consecutive_connect_failures += 1
        if (
            self._consecutive_connect_failures >= MQTT_CONNECT_FAILURES_BEFORE_REPAIR
            and self._on_connect_failed is not None
        ):
            with contextlib.suppress(Exception):
                self._on_connect_failed(error)

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
            self._fire(self._on_status, mac, mm.decode_status(payload))
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
            self._fire(self._on_base_version, mac, mm.decode_base_version(payload))
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

    async def cmd_reset_error(self, mac: str) -> None:
        """Send ROBOT_CMD_RESET_ERROR (37) — clear the current error so the
        mower can resume mowing.  Mirrors the STIGA.GO app's "Reset error".
        """
        payload = mm.encode_simple_request(mc.ROBOT_CMD_RESET_ERROR)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def cmd_settings_update(self, mac: str, settings: dict[str, Any]) -> None:
        """Send ROBOT_CMD_SETTINGS_UPDATE (18) with the given settings fields."""
        payload = mm.encode_settings_update(settings)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)

    async def cmd_schedule_set_enabled(
        self, mac: str, enabled: bool, blob: bytes | None = None
    ) -> None:
        """Toggle scheduling on/off.  Bundle blob (field 2) to preserve mowing times."""
        payload = mm.encode_schedule_enabled(enabled, blob=blob)
        await self._publish(mc.ROBOT_TOPIC_CMD_ROBOT.format(mac=mac), payload)
