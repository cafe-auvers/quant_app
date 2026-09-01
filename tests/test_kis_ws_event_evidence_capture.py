"""Safety checks for the credentialed read-only WS evidence collector."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import capture_kis_ws_event_evidence as collector
from src.api.kis_websocket import (
    KisWsDataFrame,
    KisWsProtocolOperation,
    KisWsSystemFrame,
)


def test_git_snapshot_rejects_dirty_checkout(monkeypatch):
    responses = iter(("a" * 40 + "\n", " M tests/example.py\n"))

    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout=next(responses))

    monkeypatch.setattr(collector.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="clean exact commit"):
        collector._git_snapshot()


def test_git_snapshot_returns_clean_exact_head(monkeypatch):
    responses = iter(("b" * 40 + "\n", ""))

    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout=next(responses))

    monkeypatch.setattr(collector.subprocess, "run", fake_run)

    assert collector._git_snapshot() == "b" * 40


def test_forced_reconnect_capture_requires_reack_and_resumed_data(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(collector, "_git_snapshot", lambda: "c" * 40)
    monkeypatch.setattr(collector, "_symbol_keys", lambda: {"AAPL": "DNASAAPL"})
    for name in (
        "KIS_PROD_BASE_URL",
        "KIS_PROD_APP_KEY",
        "KIS_PROD_APP_SECRET",
        "KIS_PROD_WS_URL",
    ):
        monkeypatch.setenv(name, "configured")

    class FakeClient:
        def __init__(self, **_kwargs):
            self._loop = object()
            self._subscriptions = []
            self._connection = lambda *_args: None
            self._ack = lambda *_args: None
            self._data = lambda *_args: None
            self._operation = lambda *_args: None

        def on_connection(self, callback):
            self._connection = callback

        def on_ack(self, callback):
            self._ack = callback

        def on_data(self, callback):
            self._data = callback

        def on_operation(self, callback):
            self._operation = callback

        def subscribe(self, subscriptions):
            self._subscriptions = list(subscriptions)

        def _emit_generation(self, generation):
            now = datetime.now(timezone.utc)
            for subscription in self._subscriptions:
                self._operation(
                    KisWsProtocolOperation(
                        generation=generation,
                        action="SUBSCRIBE",
                        tr_id=subscription.tr_id,
                        tr_key=subscription.tr_key,
                        sent_at=now,
                    )
                )
            self._connection(True, "", generation)
            for subscription in self._subscriptions:
                self._ack(
                    KisWsSystemFrame(
                        tr_id=subscription.tr_id,
                        tr_key=subscription.tr_key,
                        accepted=True,
                        message="SUBSCRIBE SUCCESS",
                    )
                )
                columns = (
                    collector.TRADE_COLUMNS
                    if subscription.tr_id == collector.TRADE_TR_ID
                    else collector.QUOTE_COLUMNS
                )
                self._data(
                    KisWsDataFrame(
                        tr_id=subscription.tr_id,
                        record_count=1,
                        payload="^".join("1" for _ in columns),
                        encrypted=False,
                        received_at=now,
                        payload_fingerprint=f"generation-{generation}",
                    )
                )

        def start(self):
            self._emit_generation(1)

        async def reconnect(self):
            self._connection(False, "forced test reconnect", 1)
            self._emit_generation(2)

        def stop(self):
            return None

    monkeypatch.setattr(
        collector, "KisWsApprovalKeyProvider", lambda **_kwargs: object()
    )
    monkeypatch.setattr(collector, "KisWebSocketClient", FakeClient)

    def run_now(coroutine, _loop):
        asyncio.run(coroutine)
        return SimpleNamespace()

    monkeypatch.setattr(collector.asyncio, "run_coroutine_threadsafe", run_now)

    evidence = collector.capture(
        symbol="AAPL",
        output=tmp_path / "capture.json",
        frames_per_channel=1,
        timeout_seconds=5,
        reconnect_after_seconds=1,
    )

    assert evidence["errors"] == []
    assert evidence["forced_reconnect"]["configured"] is True
    assert evidence["forced_reconnect"]["recovery_seconds"] < 10
    assert evidence["forced_reconnect"]["post_reconnect_data_channels"] == [
        collector.QUOTE_TR_ID,
        collector.TRADE_TR_ID,
    ]
    assert {item["generation"] for item in evidence["protocol_operations"]} == {
        1,
        2,
    }
