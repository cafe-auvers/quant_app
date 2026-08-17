from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

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

    asyncio.run(client._handle_raw("bad frame"))

    assert client.malformed_frame_count == 1
    assert client.is_connected() is True


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

    asyncio.run(client.run_forever())

    assert len(sockets) == 2
    assert sockets[0].sent[0]["body"]["input"]["tr_key"] == "DNASAAPL"
    assert sockets[1].sent[0]["body"]["input"]["tr_key"] == "DNASAAPL"
    assert client.reconnect_count == 1
