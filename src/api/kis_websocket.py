"""Low-level KIS WebSocket transport.

This module owns only connection and protocol mechanics.  Market-data field
semantics live in :mod:`src.services.kis_realtime_market_data`; broker fill
state remains authoritative in account reconciliation.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src.api.kis_ws_auth import KisWsApprovalKeyProvider, KisWsAuthError

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class KisWsFrameError(ValueError):
    """One malformed KIS frame; callers drop it without closing the feed."""


@dataclass(frozen=True, order=True)
class KisWsSubscription:
    tr_id: str
    tr_key: str
    symbol: str = ""
    channel: str = ""

    def __post_init__(self) -> None:
        if not self.tr_id or not self.tr_key:
            raise ValueError("tr_id and tr_key are required")


@dataclass(frozen=True)
class KisWsDataFrame:
    tr_id: str
    record_count: int
    payload: str
    encrypted: bool
    received_at: datetime
    payload_fingerprint: str


@dataclass(frozen=True)
class KisWsSystemFrame:
    tr_id: str
    tr_key: str = ""
    accepted: bool = False
    message: str = ""
    is_ping: bool = False
    is_unsubscribe: bool = False
    encrypt: str = ""
    encryption_key: str = ""
    encryption_iv: str = ""


@dataclass(frozen=True)
class KisWsProtocolOperation:
    generation: int
    action: str
    tr_id: str
    tr_key: str
    sent_at: datetime


def decode_aes_cbc_base64(*, key: str, iv: str, ciphertext: str) -> str:
    """Decode KIS's AES-CBC/Base64 execution-notice envelope."""
    try:
        cipher = Cipher(
            algorithms.AES(str(key).encode("utf-8")),
            modes.CBC(str(iv).encode("utf-8")),
        )
        decryptor = cipher.decryptor()
        padded = decryptor.update(base64.b64decode(ciphertext)) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
    except Exception as exc:
        raise KisWsFrameError(f"encrypted KIS frame could not be decoded: {exc}") from exc


def parse_kis_ws_frame(raw: str, *, received_at: Optional[datetime] = None):
    """Parse a realtime data envelope or JSON system/ACK/PING frame."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str) or not raw:
        raise KisWsFrameError("empty KIS WebSocket frame")
    observed_at = received_at or _utc_now()
    if raw[0] in {"0", "1"}:
        parts = raw.split("|", 3)
        if len(parts) != 4:
            raise KisWsFrameError("realtime frame must contain four pipe-delimited fields")
        try:
            record_count = int(parts[2])
        except ValueError as exc:
            raise KisWsFrameError("realtime frame record count is not an integer") from exc
        if record_count <= 0 or not parts[1] or not parts[3]:
            raise KisWsFrameError("realtime frame has an empty TR ID, count, or payload")
        return KisWsDataFrame(
            tr_id=parts[1],
            record_count=record_count,
            payload=parts[3],
            encrypted=parts[0] == "1",
            received_at=observed_at,
            payload_fingerprint=hashlib.sha256(parts[3].encode("utf-8")).hexdigest(),
        )

    try:
        payload = json.loads(raw)
        header = payload["header"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise KisWsFrameError("system frame is not valid KIS JSON") from exc
    tr_id = str(header.get("tr_id") or "")
    if not tr_id:
        raise KisWsFrameError("system frame omitted header.tr_id")
    if tr_id == "PINGPONG":
        return KisWsSystemFrame(tr_id=tr_id, is_ping=True)
    body = payload.get("body") or {}
    output = body.get("output") or {}
    message = str(body.get("msg1") or "")
    return KisWsSystemFrame(
        tr_id=tr_id,
        tr_key=str(header.get("tr_key") or ""),
        accepted=str(body.get("rt_cd") or "") == "0",
        message=message,
        is_unsubscribe=message.upper().startswith("UNSUB"),
        encrypt=str(header.get("encrypt") or ""),
        encryption_key=str(output.get("key") or ""),
        encryption_iv=str(output.get("iv") or ""),
    )


DataCallback = Callable[[KisWsDataFrame], None]
AckCallback = Callable[[KisWsSystemFrame], None]
ConnectionCallback = Callable[[bool, str, int], None]
OperationCallback = Callable[[KisWsProtocolOperation], None]
CriticalCallback = Callable[[str], None]


class KisWebSocketClient:
    """Reconnectable KIS transport with durable in-memory desired state."""

    def __init__(
        self,
        *,
        url: str,
        approval_keys: KisWsApprovalKeyProvider,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        reconnect_jitter_seconds: float = 0.5,
        connect_factory: Optional[Callable[..., Any]] = None,
        clock: Callable[[], datetime] = _utc_now,
        random_source: Callable[[], float] = random.random,
        critical_alert: CriticalCallback = lambda message: None,
    ) -> None:
        self._url = str(url or "")
        self._approval_keys = approval_keys
        self._initial_backoff = max(0.0, float(reconnect_initial_seconds))
        self._max_backoff = max(self._initial_backoff, float(reconnect_max_seconds))
        self._jitter = max(0.0, float(reconnect_jitter_seconds))
        self._connect_factory = connect_factory
        self._clock = clock
        self._random = random_source
        self._critical_alert = critical_alert
        self._desired: Dict[Tuple[str, str], KisWsSubscription] = {}
        self._desired_lock = threading.Lock()
        self._data_callbacks: list[DataCallback] = []
        self._ack_callbacks: list[AckCallback] = []
        self._connection_callbacks: list[ConnectionCallback] = []
        self._operation_callbacks: list[OperationCallback] = []
        self._encryption: Dict[str, Tuple[str, str]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._socket = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = False
        self._reconnect_generation = 0
        self.malformed_frame_count = 0
        self.reconnect_count = 0

    def on_data(self, callback: DataCallback) -> None:
        self._data_callbacks.append(callback)

    def on_ack(self, callback: AckCallback) -> None:
        self._ack_callbacks.append(callback)

    def on_connection(self, callback: ConnectionCallback) -> None:
        self._connection_callbacks.append(callback)

    def on_operation(self, callback: OperationCallback) -> None:
        self._operation_callbacks.append(callback)

    def is_connected(self) -> bool:
        return self._connected

    def desired_subscriptions(self) -> list[KisWsSubscription]:
        with self._desired_lock:
            return sorted(self._desired.values())

    def subscribe(self, subscriptions: Iterable[KisWsSubscription]) -> None:
        additions = []
        with self._desired_lock:
            for subscription in subscriptions:
                key = (subscription.tr_id, subscription.tr_key)
                if key not in self._desired:
                    additions.append(subscription)
                self._desired[key] = subscription
        self._schedule_messages(additions, tr_type="1")

    def unsubscribe(self, subscriptions: Iterable[KisWsSubscription]) -> None:
        removals = []
        with self._desired_lock:
            for subscription in subscriptions:
                removed = self._desired.pop((subscription.tr_id, subscription.tr_key), None)
                if removed is not None:
                    removals.append(removed)
        self._schedule_messages(removals, tr_type="2")

    def forget_subscriptions(
        self, subscriptions: Iterable[KisWsSubscription]
    ) -> None:
        """Drop NACKed subscriptions from reconnect replay without UNSUB.

        A rejected subscribe never became active at KIS, so sending an
        unsubscribe would manufacture a second protocol operation. The
        market-data coordinator calls this only after an explicit NACK.
        """

        with self._desired_lock:
            for subscription in subscriptions:
                self._desired.pop((subscription.tr_id, subscription.tr_key), None)

    def _schedule_messages(self, subscriptions, *, tr_type: str) -> None:
        if not subscriptions or not self._connected or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._send_subscriptions(list(subscriptions), tr_type=tr_type), self._loop
        )

    def _subscription_payload(
        self, subscription: KisWsSubscription, *, tr_type: str, approval_key: str
    ) -> str:
        return json.dumps(
            {
                "header": {
                    "approval_key": approval_key,
                    "custtype": "P",
                    "tr_type": tr_type,
                    "content-type": "utf-8",
                },
                "body": {
                    "input": {
                        "tr_id": subscription.tr_id,
                        "tr_key": subscription.tr_key,
                    }
                },
            },
            separators=(",", ":"),
        )

    async def _send_subscriptions(self, subscriptions, *, tr_type: str) -> None:
        socket = self._socket
        if socket is None:
            return
        approval = await asyncio.to_thread(self._approval_keys.get)
        for subscription in subscriptions:
            await socket.send(
                self._subscription_payload(
                    subscription, tr_type=tr_type, approval_key=approval.value
                )
            )
            operation = KisWsProtocolOperation(
                generation=self._reconnect_generation,
                action="SUBSCRIBE" if tr_type == "1" else "UNSUBSCRIBE",
                tr_id=subscription.tr_id,
                tr_key=subscription.tr_key,
                sent_at=self._clock(),
            )
            for callback in list(self._operation_callbacks):
                try:
                    callback(operation)
                except Exception:
                    logger.exception("KIS WebSocket operation callback failed")
            await asyncio.sleep(0.05)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._url:
            raise ValueError("KIS WebSocket URL is not configured")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self.run_forever()),
            name="KisWebSocketClient",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop is not None and self._socket is not None:
            asyncio.run_coroutine_threadsafe(self._socket.close(), self._loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    async def reconnect(self) -> None:
        if self._socket is not None:
            await self._socket.close()

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        attempts = 0
        while not self._stop_event.is_set():
            try:
                # Fail before connecting if approval issuance or the live
                # capability gate is unavailable.
                self._approval_keys.get()
                connect = self._connect_factory
                if connect is None:
                    import websockets

                    connect = websockets.connect
                async with connect(self._url, ping_interval=None) as socket:
                    self._socket = socket
                    self._connected = True
                    self._reconnect_generation += 1
                    if self._reconnect_generation > 1:
                        self.reconnect_count += 1
                    attempts = 0
                    # Desired, not merely previously ACKed, subscriptions are
                    # restored after every reconnect.
                    await self._send_subscriptions(
                        self.desired_subscriptions(), tr_type="1"
                    )
                    # Observers see the new session only after every desired
                    # subscription request has crossed the socket boundary.
                    self._notify_connection(True, "", self._reconnect_generation)
                    async for raw in socket:
                        await self._handle_raw(raw)
                    if not self._stop_event.is_set():
                        self._notify_connection(
                            False,
                            "KIS WebSocket stream ended",
                            self._reconnect_generation,
                        )
            except KisWsAuthError as exc:
                self._critical_alert(str(exc))
                self._notify_connection(False, str(exc), self._reconnect_generation)
                break
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                attempts += 1
                self._notify_connection(False, str(exc), self._reconnect_generation)
                delay = min(
                    self._max_backoff,
                    self._initial_backoff * (2 ** max(0, attempts - 1)),
                ) + self._random() * self._jitter
                await asyncio.sleep(delay)
            finally:
                self._connected = False
                self._socket = None
        self._loop = None

    async def _handle_raw(self, raw: str) -> None:
        try:
            frame = parse_kis_ws_frame(raw, received_at=self._clock())
            if isinstance(frame, KisWsSystemFrame):
                if frame.is_ping:
                    if self._socket is not None:
                        payload = raw.encode("utf-8") if isinstance(raw, str) else raw
                        await self._socket.pong(payload)
                    return
                if frame.encryption_key and frame.encryption_iv:
                    self._encryption[frame.tr_id] = (
                        frame.encryption_key,
                        frame.encryption_iv,
                    )
                for callback in list(self._ack_callbacks):
                    callback(frame)
                if not frame.accepted and any(
                    token in frame.message.lower()
                    for token in ("approval", "auth", "token", "인증")
                ):
                    self._approval_keys.invalidate()
                    if self._socket is not None:
                        await self._socket.close()
                return
            if frame.encrypted:
                key_iv = self._encryption.get(frame.tr_id)
                if key_iv is None:
                    raise KisWsFrameError(
                        f"encrypted {frame.tr_id} frame arrived before its key/IV ACK"
                    )
                frame = KisWsDataFrame(
                    tr_id=frame.tr_id,
                    record_count=frame.record_count,
                    payload=decode_aes_cbc_base64(
                        key=key_iv[0], iv=key_iv[1], ciphertext=frame.payload
                    ),
                    encrypted=True,
                    received_at=frame.received_at,
                    payload_fingerprint=frame.payload_fingerprint,
                )
            for callback in list(self._data_callbacks):
                callback(frame)
        except Exception:
            self.malformed_frame_count += 1
            logger.exception("Malformed KIS WebSocket frame dropped; connection remains open")

    def _notify_connection(self, connected: bool, reason: str, generation: int) -> None:
        for callback in list(self._connection_callbacks):
            try:
                callback(connected, reason, generation)
            except Exception:
                logger.exception("KIS WebSocket connection callback failed")
