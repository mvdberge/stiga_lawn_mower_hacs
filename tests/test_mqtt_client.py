"""Tests for :mod:`mqtt_client`.

The transport layer (`aiomqtt`) is replaced with a fake so these tests
exercise topic dispatch, subscription building, and the publish guard
without spinning up a real broker. End-to-end tests against a local MQTT
broker live behind the `requires_broker` marker and run separately.
"""

from __future__ import annotations

import json
import ssl
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.stiga_mower import mqtt_client as mc_mod
from custom_components.stiga_mower import mqtt_constants as mc
from custom_components.stiga_mower import mqtt_messages as mm
from custom_components.stiga_mower import protobuf_codec as pb

ROBOT_MAC = "AA:BB:CC:DD:EE:01"
BASE_MAC = "AA:BB:CC:DD:EE:02"


# ---------------------------------------------------------------- Fixtures


@pytest.fixture
def client(hass) -> mc_mod.StigaMQTT:
    async def _token() -> str:
        return "fake-token"

    c = mc_mod.StigaMQTT(hass, _token, broker_id="broker")
    c.add_robot(ROBOT_MAC)
    c.add_base(BASE_MAC)
    return c


# ---------------------------------------------------------------- Subscriptions


def test_subscriptions_cover_robot_and_base(client: mc_mod.StigaMQTT) -> None:
    topics = client._subscriptions()
    assert f"{ROBOT_MAC}/LOG/+" in topics
    assert f"{ROBOT_MAC}/JSON_NOTIFICATION" in topics
    assert f"CMD_ROBOT_ACK/{ROBOT_MAC}" in topics
    assert f"{BASE_MAC}/LOG/+" in topics
    assert f"{BASE_MAC}/JSON_NOTIFICATION" in topics
    assert f"CMD_REFERENCE_ACK/{BASE_MAC}" in topics


def test_broker_host_uses_template(client: mc_mod.StigaMQTT) -> None:
    assert client.broker_host == "robot-mqtt-broker.stiga.com"


def test_broker_host_per_account_id(hass) -> None:
    c = mc_mod.StigaMQTT(hass, AsyncMock(return_value="t"), broker_id="acc-42")
    assert c.broker_host == "robot-mqtt-acc-42.stiga.com"


# ---------------------------------------------------------------- Dispatch


def test_dispatch_robot_status_invokes_callback(client: mc_mod.StigaMQTT) -> None:
    received: list[tuple[str, dict]] = []
    client.set_handlers(on_status=lambda mac, data: received.append((mac, data)))

    payload = pb.encode({1: 1, 3: 4})  # status_valid + DOCKED
    client._dispatch(f"{ROBOT_MAC}/LOG/STATUS", payload)

    assert len(received) == 1
    mac, data = received[0]
    assert mac == ROBOT_MAC
    assert data == {"status_valid": True, "status_type": "DOCKED"}


def test_dispatch_robot_position_invokes_callback(client: mc_mod.StigaMQTT) -> None:
    received: list[tuple[str, dict]] = []
    client.set_handlers(on_position=lambda mac, data: received.append((mac, data)))

    # Build a position frame manually (FIXED64 fields not emittable via codec)
    import struct

    payload = b""
    for field, value in [(1, 1.0), (2, 2.0), (3, 0.5)]:
        payload += bytes([(field << 3) | 1]) + struct.pack("<d", value)
    client._dispatch(f"{ROBOT_MAC}/LOG/ROBOT_POSITION", payload)

    assert received == [
        (
            ROBOT_MAC,
            {
                "lon_offset_m": 1.0,
                "lat_offset_m": 2.0,
                "orientation_rad": 0.5,
            },
        )
    ]


def test_dispatch_settings_invokes_callback(client: mc_mod.StigaMQTT) -> None:
    received: list = []
    client.set_handlers(on_settings=lambda mac, data: received.append((mac, data)))

    payload = pb.encode({6: 1, 7: 0})  # anti_theft on, smart_cut off
    client._dispatch(f"{ROBOT_MAC}/LOG/SETTINGS", payload)
    assert received == [(ROBOT_MAC, {"anti_theft": True, "smart_cutting_height": False})]


def test_dispatch_schedule_invokes_callback(client: mc_mod.StigaMQTT) -> None:
    received: list = []
    client.set_handlers(on_schedule=lambda mac, data: received.append((mac, data)))

    # 42-byte blob = 7 days × 6 zero-varints (confirmed Phase 6b layout)
    payload = pb.encode({1: 1, 2: bytes(42), 4: 5})
    client._dispatch(f"{ROBOT_MAC}/LOG/SCHEDULING_SETTINGS", payload)
    assert received[0][0] == ROBOT_MAC
    assert received[0][1]["enabled"] is True
    assert len(received[0][1]["days"]) == 7


def test_dispatch_base_status_routes_to_base_handler(client: mc_mod.StigaMQTT) -> None:
    """Identical topic shape (.../LOG/STATUS); registry lookup picks the codec."""
    robot_received: list = []
    base_received: list = []
    client.set_handlers(
        on_status=lambda *a: robot_received.append(a),
        on_base_status=lambda *a: base_received.append(a),
    )

    payload = pb.encode({1: 5, 4: 1, 10: 1})
    client._dispatch(f"{BASE_MAC}/LOG/STATUS", payload)

    assert robot_received == []
    assert base_received[0][0] == BASE_MAC
    assert base_received[0][1]["status_type"] == "PUBLISHING_CORRECTIONS"


def test_dispatch_notification_decodes_json(client: mc_mod.StigaMQTT) -> None:
    received: list = []
    client.set_handlers(on_notification=lambda mac, data: received.append((mac, data)))

    body = {"title": "Stuck", "data": {"type": "blocked_error"}}
    client._dispatch(f"{ROBOT_MAC}/JSON_NOTIFICATION", json.dumps(body).encode())
    assert received == [(ROBOT_MAC, body)]


def test_dispatch_command_ack_decodes(client: mc_mod.StigaMQTT) -> None:
    received: list = []
    client.set_handlers(on_command_ack=lambda mac, data: received.append((mac, data)))

    payload = pb.encode({1: mc.ROBOT_CMD_START, 2: 1})
    client._dispatch(f"CMD_ROBOT_ACK/{ROBOT_MAC}", payload)

    mac_seen, data = received[0]
    assert mac_seen == ROBOT_MAC
    assert data == {
        "cmd_type": mc.ROBOT_CMD_START,
        "cmd_name": "START",
        "result": 1,
        "ok": True,
    }


def test_dispatch_unregistered_mac_is_ignored(client: mc_mod.StigaMQTT) -> None:
    """Topics for MACs we never registered must not crash the dispatcher."""
    called: list = []
    client.set_handlers(on_status=lambda *a: called.append(a))
    client._dispatch("11:22:33:44:55:66/LOG/STATUS", pb.encode({3: 4}))
    assert called == []


def test_dispatch_unknown_log_kind_is_logged_not_raised(
    client: mc_mod.StigaMQTT,
) -> None:
    client._dispatch(f"{ROBOT_MAC}/LOG/MYSTERY", b"\x00")
    # No exception; nothing else to assert beyond "didn't crash".


def test_dispatch_handler_exception_does_not_propagate(
    client: mc_mod.StigaMQTT,
) -> None:
    def boom(mac: str, data: dict) -> None:
        raise RuntimeError("oops")

    client.set_handlers(on_status=boom)
    # Must not raise — coordinator bugs should never kill the dispatch loop.
    client._dispatch(f"{ROBOT_MAC}/LOG/STATUS", pb.encode({3: 4}))


# ---------------------------------------------------------------- Connection state


def test_set_connected_fires_callback_only_on_change(
    client: mc_mod.StigaMQTT,
) -> None:
    seen: list[bool] = []
    client.set_handlers(on_connection_change=seen.append)
    client._set_connected(True)
    client._set_connected(True)  # idempotent
    client._set_connected(False)
    client._set_connected(False)  # idempotent
    assert seen == [True, False]


def test_set_connected_swallows_callback_errors(client: mc_mod.StigaMQTT) -> None:
    def boom(_: bool) -> None:
        raise RuntimeError("nope")

    client.set_handlers(on_connection_change=boom)
    client._set_connected(True)  # must not raise
    assert client.connected is True


# ---------------------------------------------------------------- Publish guard


async def test_request_status_raises_when_not_connected(
    client: mc_mod.StigaMQTT,
) -> None:
    with pytest.raises(mc_mod.StigaMQTTError, match="not connected"):
        await client.request_status(ROBOT_MAC)


async def test_request_status_publishes_to_cmd_robot_topic(
    client: mc_mod.StigaMQTT,
) -> None:
    fake = AsyncMock()
    client._client = MagicMock(publish=fake)
    client._connected = True

    await client.request_status(
        ROBOT_MAC, battery=True, mowing=False, location=False, network=False
    )

    assert fake.await_count == 1
    args, kwargs = fake.call_args
    assert args[0] == f"{ROBOT_MAC}/CMD_ROBOT"
    expected_payload = mm.encode_status_request(
        battery=True,
        mowing=False,
        location=False,
        network=False,
    )
    assert kwargs["payload"] == expected_payload
    assert kwargs["qos"] == 2


async def test_request_position_uses_cmd_22(client: mc_mod.StigaMQTT) -> None:
    fake = AsyncMock()
    client._client = MagicMock(publish=fake)
    client._connected = True

    await client.request_position(ROBOT_MAC)

    expected = mm.encode_simple_request(mc.ROBOT_CMD_POSITION_REQUEST)
    assert fake.call_args.kwargs["payload"] == expected


async def test_request_settings_and_schedule_use_correct_cmd_ids(
    client: mc_mod.StigaMQTT,
) -> None:
    fake = AsyncMock()
    client._client = MagicMock(publish=fake)
    client._connected = True

    await client.request_settings(ROBOT_MAC)
    await client.request_schedule(ROBOT_MAC)

    payloads = [call.kwargs["payload"] for call in fake.await_args_list]
    assert payloads[0] == mm.encode_simple_request(mc.ROBOT_CMD_SETTINGS_REQUEST)
    assert payloads[1] == mm.encode_simple_request(
        mc.ROBOT_CMD_SCHEDULING_SETTINGS_REQUEST,
    )


# ---------------------------------------------------------------- SSL context


def test_build_ssl_loads_bundled_cert(client: mc_mod.StigaMQTT) -> None:
    ctx = client._build_ssl()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


# ---------------------------------------------------------------- Run-loop integration


class _FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _FakeMessages:
    """Async iterator yielding pre-recorded MQTT messages then closing.

    A real :class:`aiomqtt.Client.messages` iterator stays open until the
    connection ends; for unit tests we fake the connection ending right
    after the recorded batch so the session block exits cleanly without
    needing an external stop signal.
    """

    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = list(messages)

    def __aiter__(self) -> AsyncIterator[_FakeMessage]:
        return self

    async def __anext__(self) -> _FakeMessage:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _FakeAiomqttClient:
    """Minimal aiomqtt.Client stand-in for run-loop integration tests."""

    def __init__(self, messages: list[_FakeMessage]) -> None:
        self.subscribe = AsyncMock()
        self.publish = AsyncMock()
        self.messages = _FakeMessages(messages)
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> _FakeAiomqttClient:
        self.entered += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited += 1


async def test_connect_session_subscribes_and_dispatches(
    client: mc_mod.StigaMQTT,
) -> None:
    """One session: subscribe to all topics, dispatch one status frame."""
    received: list = []
    client.set_handlers(on_status=lambda mac, data: received.append((mac, data)))

    payload = pb.encode({1: 1, 3: 4})  # status_valid + DOCKED
    fake_client = _FakeAiomqttClient([_FakeMessage(f"{ROBOT_MAC}/LOG/STATUS", payload)])

    with patch.object(mc_mod.aiomqtt, "Client", return_value=fake_client):
        await client._connect_session()

    assert fake_client.entered == 1
    assert fake_client.exited == 1
    # Subscribe was called once per registered topic
    assert fake_client.subscribe.await_count == len(client._subscriptions())
    assert received == [(ROBOT_MAC, {"status_valid": True, "status_type": "DOCKED"})]


async def test_connect_session_marks_disconnected_on_exit(
    client: mc_mod.StigaMQTT,
) -> None:
    seen: list[bool] = []
    client.set_handlers(on_connection_change=seen.append)

    fake_client = _FakeAiomqttClient([])
    with patch.object(mc_mod.aiomqtt, "Client", return_value=fake_client):
        await client._connect_session()

    # The connection-change callback never fires from `_connect_session` on
    # exit (the outer `_run_loop` owns the disconnected transition).
    assert seen == [True]
    # And the session has cleared the client handle so publishes get rejected.
    assert client._client is None
