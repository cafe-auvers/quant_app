from __future__ import annotations

import asyncio
import base64
import datetime as dt
import gc
import json

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close
import src.api.kis_websocket as kis_websocket_module

from src.api.kis_websocket import (
    KisWebSocketClient,
    KisWsDataFrame,
    KisWsFrameError,
    KisWsSubscription,
    KisWsSystemFrame,
    decode_aes_cbc_base64,
    parse_kis_ws_frame,
)


NOW = dt.datetime(2026, 8, 16, 1, 0, tzinfo=dt.timezone.utc)


def _run_async(awaitable):
    with asyncio.Runner(
        loop_factory=kis_websocket_module._websocket_event_loop
    ) as runner:
        return runner.run(awaitable)


def test_parse_realtime_frame_preserves_count_and_payload_identity():
    frame = parse_kis_ws_frame("0|HDFSCNT0|1|AAPL^100", received_at=NOW)
    assert isinstance(frame, KisWsDataFrame)
    assert frame.tr_id == "HDFSCNT0"
    assert frame.record_count == 1
    assert frame.payload == "AAPL^100"
    assert len(frame.payload_fingerprint) == 64


def test_parse_ack_nack_and_ping():
    ack = parse_kis_ws_frame(
        json.dumps(
            {
                "header": {"tr_id": "HDFSCNT0", "tr_key": "DNASAAPL"},
                "body": {"rt_cd": "0", "msg1": "SUBSCRIBE SUCCESS"},
            }
        )
    )
    ping = parse_kis_ws_frame(json.dumps({"header": {"tr_id": "PINGPONG"}}))
    assert isinstance(ack, KisWsSystemFrame) and ack.accepted
    assert isinstance(ping, KisWsSystemFrame) and ping.is_ping


def test_single_session_nack_preserves_code_and_closes_rejected_socket():
    client = KisWebSocketClient(url="ws://example", approval_keys=_Keys())

    class _Socket:
        closed = False

        async def close(self):
            self.closed = True

    socket = _Socket()
    client._socket = socket
    raw = json.dumps(
        {
            "header": {"tr_id": "(null)", "tr_key": ""},
            "body": {
                "rt_cd": "9",
                "msg_cd": "OPSP8996",
                "msg1": "ALREADY IN USE appkey",
            },
        }
    )

    _run_async(client._handle_raw(raw))

    frame = parse_kis_ws_frame(raw)
    assert frame.message_code == "OPSP8996"
    assert socket.closed is True


@pytest.mark.parametrize("raw", ["", "0|HDFSCNT0|bad|x", "{not-json"])
def test_malformed_frames_are_rejected_individually(raw):
    with pytest.raises(KisWsFrameError):
        parse_kis_ws_frame(raw)


def test_encrypted_execution_notice_decoder_matches_aes_cbc_pkcs7():
    key = "0123456789abcdef0123456789abcdef"
    iv = "0123456789abcdef"
    plaintext = "ACCOUNT_REDACTED^ORDER_REDACTED^10^99.5"
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(key.encode()), modes.CBC(iv.encode())
    ).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()

    assert decode_aes_cbc_base64(
        key=key, iv=iv, ciphertext=base64.b64encode(encrypted).decode()
    ) == plaintext


class _Approval:
    value = "approval"


class _Keys:
    def get(self, **kwargs):
        return _Approval()


def test_windows_transport_uses_private_selector_loop(monkeypatch):
    monkeypatch.setattr(kis_websocket_module.sys, "platform", "win32")

    loop = kis_websocket_module._websocket_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_windows_selector_loop_recovers_from_transient_socketpair_failure(
    monkeypatch,
):
    real_socketpair = kis_websocket_module.socket.socketpair
    attempts = []

    def flaky_socketpair(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError(10014, "temporary socket-pair startup failure")
        return real_socketpair(*args, **kwargs)

    monkeypatch.setattr(kis_websocket_module.socket, "socketpair", flaky_socketpair)
    monkeypatch.setattr(kis_websocket_module, "_EVENT_LOOP_RETRY_SECONDS", 0)

    loop = kis_websocket_module._ResilientWindowsSelectorEventLoop()
    try:
        # One injected failure must be recovered.  Windows itself may report
        # another transient socket-pair failure during a busy test run.
        assert len(attempts) >= 2
    finally:
        loop.close()


def test_windows_selector_loop_exhausted_startup_cleans_partial_loop(
    monkeypatch,
):
    attempts = []

    def failing_socketpair(*args, **kwargs):
        attempts.append(True)
        raise OSError(10014, "persistent socket-pair startup failure")

    monkeypatch.setattr(
        kis_websocket_module.socket, "socketpair", failing_socketpair
    )
    monkeypatch.setattr(kis_websocket_module, "_EVENT_LOOP_RETRY_SECONDS", 0)

    def construct_failed_loop():
        with pytest.raises(OSError, match="persistent socket-pair"):
            kis_websocket_module._ResilientWindowsSelectorEventLoop()

    construct_failed_loop()
    gc.collect()

    assert len(attempts) == kis_websocket_module._EVENT_LOOP_START_ATTEMPTS


def test_thread_bootstrap_retries_before_creating_coroutine():
    attempts = []

    def loop_factory():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError(10014, "temporary socket-pair startup failure")
        return kis_websocket_module._ResilientWindowsSelectorEventLoop()

    client = KisWebSocketClient(
        url="ws://example",
        approval_keys=_Keys(),
        event_loop_factory=loop_factory,
    )

    async def finish_immediately():
        client._stop_event.set()

    client.run_forever = finish_immediately
    client._run_thread()

    assert len(attempts) == 2


def test_desired_subscriptions_survive_while_disconnected():
    client = KisWebSocketClient(url="ws://example", approval_keys=_Keys())
    subscription = KisWsSubscription("HDFSCNT0", "DNASAAPL", "AAPL", "TRADE")
    client.subscribe([subscription])

    assert client.desired_subscriptions() == [subscription]


def test_explicit_nack_forget_removes_reconnect_intent_without_unsubscribe():
    client = KisWebSocketClient(url="ws://example", approval_keys=_Keys())
    subscription = KisWsSubscription("HDFSCNT0", "DNASAAPL", "AAPL", "TRADE")
    client.subscribe([subscription])

    client.forget_subscriptions([subscription])

    assert client.desired_subscriptions() == []


def test_malformed_frame_is_dropped_without_changing_connected_state():
    client = KisWebSocketClient(url="ws://example", approval_keys=_Keys())
    client._connected = True

    _run_async(client._handle_raw("bad frame"))

    assert client.malformed_frame_count == 1
    assert client.is_connected() is True


def test_clean_close_during_ping_reply_is_not_counted_as_malformed():
    client = KisWebSocketClient(url="ws://example", approval_keys=_Keys())

    class _ClosingSocket:
        async def pong(self, _payload):
            close = Close(1000, "OK")
            raise ConnectionClosedOK(close, close, True)

    client._socket = _ClosingSocket()
    ping = json.dumps({"header": {"tr_id": "PINGPONG"}})

    with pytest.raises(ConnectionClosedOK):
        _run_async(client._handle_raw(ping))

    assert client.malformed_frame_count == 0


def test_reconnect_resubscribes_every_desired_subscription():
    sockets = []
    client = None

    class _Socket:
        def __init__(self, fail):
            self.fail = fail
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.fail:
                self.fail = False
                raise ConnectionError("forced reconnect")
            client._stop_event.set()
            raise StopAsyncIteration

        async def send(self, payload):
            self.sent.append(json.loads(payload))

        async def close(self):
            return None

    def connect(url, **kwargs):
        socket = _Socket(fail=not sockets)
        sockets.append(socket)
        return socket

    client = KisWebSocketClient(
        url="ws://example",
        approval_keys=_Keys(),
        connect_factory=connect,
        reconnect_initial_seconds=0,
        reconnect_max_seconds=0,
        reconnect_jitter_seconds=0,
    )
    subscription = KisWsSubscription("HDFSCNT0", "DNASAAPL", "AAPL", "TRADE")
    client.subscribe([subscription])
    operations = []
    client.on_operation(operations.append)

    _run_async(client.run_forever())

    assert len(sockets) == 2
    assert sockets[0].sent[0]["body"]["input"]["tr_key"] == "DNASAAPL"
    assert sockets[1].sent[0]["body"]["input"]["tr_key"] == "DNASAAPL"
    assert client.reconnect_count == 1
    assert [(item.generation, item.action) for item in operations] == [
        (1, "SUBSCRIBE"),
        (2, "SUBSCRIBE"),
    ]


def test_reader_captures_session_nack_while_subscription_replay_is_sending():
    client = None
    nack = json.dumps(
        {
            "header": {"tr_id": "(null)", "tr_key": ""},
            "body": {
                "rt_cd": "9",
                "msg_cd": "OPSP8996",
                "msg1": "ALREADY IN USE appkey",
            },
        }
    )

    class _Socket:
        def __init__(self):
            self.sent = []
            self.first_send = asyncio.Event()
            self.nack_delivered = False
            self.closed_by_server = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.first_send.wait()
            if self.nack_delivered:
                raise StopAsyncIteration
            self.nack_delivered = True
            return nack

        async def send(self, payload):
            if self.closed_by_server:
                raise ConnectionError("server closed during subscription replay")
            self.sent.append(json.loads(payload))
            self.first_send.set()
            # Model KIS closing immediately after rejecting the first replayed
            # subscription. The reader must already be active to preserve the
            # NACK before the next send observes that close.
            await asyncio.sleep(0)
            self.closed_by_server = True

        async def close(self):
            self.closed_by_server = True
            client._stop_event.set()

    socket = _Socket()

    def connect(url, **kwargs):
        return socket

    client = KisWebSocketClient(
        url="ws://example",
        approval_keys=_Keys(),
        connect_factory=connect,
        reconnect_initial_seconds=0,
        reconnect_max_seconds=0,
        reconnect_jitter_seconds=0,
    )
    client.subscribe(
        [
            KisWsSubscription("HDFSCNT0", "DNASAAPL", "AAPL", "TRADE"),
            KisWsSubscription("HDFSASP0", "DNASAAPL", "AAPL", "QUOTE"),
        ]
    )
    frames = []
    client.on_ack(frames.append)

    _run_async(client.run_forever())

    assert len(socket.sent) == 1
    assert len(frames) == 1
    assert frames[0].message_code == "OPSP8996"
    assert frames[0].message == "ALREADY IN USE appkey"
