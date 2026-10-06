from datetime import datetime, timezone
import json

import pytest

from src.core.trade_card_state import TradeCardState
from src.services import live_session_checks as checks
from src.services.realtime_market_data import QuoteSnapshot


SHA = "a" * 40
DAY = "2026-10-05"


def test_evidence_path_cannot_touch_repository():
    with pytest.raises(ValueError, match="outside the repository"):
        checks.LiveSessionChecks(checks.ROOT_DIR / "data", SHA, DAY, start_thread=False)


def test_card_snapshot_is_detached_and_disabled_capture_is_empty(monkeypatch):
    card = TradeCardState(environment="PROD", account_no="test", symbol="SHMD")
    monkeypatch.setattr(checks, "checks_enabled", lambda: True)
    snapshot = checks.prepare_card_snapshot([card])
    card.breakout_price = 4.65
    assert snapshot[0]["breakout_price"] is None
    snapshot[0]["orb_candidate_states"]["changed"] = True
    assert card.orb_candidate_states == {}
    monkeypatch.setattr(checks, "checks_enabled", lambda: False)
    assert checks.prepare_card_snapshot([card]) == ()


def test_queue_is_bounded_and_loss_is_visible(tmp_path):
    observer = checks.LiveSessionChecks(tmp_path, SHA, DAY, queue_size=1, start_thread=False)
    observer.enqueue("A", {})
    observer.enqueue("B", {})
    assert observer.dropped == 1
    assert observer.queue.qsize() == 1
    observer.finished = True
    observer.enqueue("C", {})
    assert observer.queue.qsize() == 1


def test_snapshot_feed_preserves_stale_rejection_and_has_no_transport():
    now = datetime.now(timezone.utc)
    market = checks.SnapshotMarketData()
    market.connected = True
    market.quotes["SHMD"] = QuoteSnapshot(symbol="SHMD", last_price=4.7, ask=4.71, received_at=now)
    market.states["SHMD"] = {"trade_acked": True, "quote_acked": True,
                             "last_trade_event_at": now, "last_quote_event_at": now}
    assert market.entry_quote_ready("SHMD", now=now)
    assert not market.entry_quote_ready("SHMD", now=now.replace(year=now.year + 1))
    market.states["SHMD"]["quote_rejected_due_to_capacity"] = True
    assert not market.entry_quote_ready("SHMD", now=now)
    with pytest.raises(NotImplementedError):
        market.reconnect()
    with pytest.raises(NotImplementedError):
        market.subscribe(["SHMD"])


def test_input_capture_never_drains_or_reconnects_and_reports_failures(tmp_path, monkeypatch):
    from src.services.kis_realtime_market_data import SymbolFeedState
    observer = checks.LiveSessionChecks(tmp_path, SHA, DAY, start_thread=False)
    monkeypatch.setattr(checks, "configured_observer", lambda: observer)
    monkeypatch.setattr(checks, "_observer", observer)
    observer._last_sample = float("inf")
    card = TradeCardState(environment="PROD", account_no="test", symbol="SHMD")
    quote = QuoteSnapshot(symbol="SHMD", last_price=4.7)

    class Feed:
        def is_connected(self): return True
        def symbol_state(self, symbol): return SymbolFeedState(symbol=symbol)
        def latest_quote(self, symbol): return quote
        def poll_once(self): raise AssertionError("collector must never drain feed")
        def reconnect(self): raise AssertionError("collector must never disconnect feed")

    checks.observe_cycle(Feed(), [quote], (card.to_dict(),), lambda *_args: 1234)
    kind, payload = observer.queue.get_nowait()
    assert kind == "CYCLE"
    assert payload[0] == (quote,)
    assert payload[3] == 1234

    class BrokenFeed(Feed):
        def is_connected(self): raise OSError("unavailable")

    checks.observe_cycle(BrokenFeed(), [quote], (card.to_dict(),), lambda *_args: 1234)
    assert observer.errors["input_capture:OSError"] == 1


def test_shadow_runtime_and_report_do_not_certify_gates(tmp_path):
    from src.core.execution_request import SubmitExecutionRequest
    from src.core.order_state import OrderIntent, OrderSide
    from gate3.shadow_boundary import ShadowMutationIntercepted
    observer = checks.LiveSessionChecks(tmp_path, SHA, DAY, start_thread=False)
    observer._initialize()
    card = TradeCardState(environment="PROD", account_no="test", symbol="SHMD")
    observer._cycle(((), (card.to_dict(),), {"captured_at": datetime.now(timezone.utc),
                     "connected": False, "states": {}, "latest_quotes": {}}, 1234))
    try:
        assert observer.shadow is not None
        with pytest.raises(ShadowMutationIntercepted):
            observer.shadow.gateway.submit_guarded(SubmitExecutionRequest(
                client_order_id="isolated-test", environment="PROD", account_no="test",
                symbol="SHMD", side=OrderSide.BUY, intent=OrderIntent.ENTRY,
                quantity=1, limit_price=4.65))
        observer._write_report()
        report = json.loads((tmp_path / "checks_report.json").read_text())
        assert report["formal_gate_result"] == "NOT_CERTIFIED"
        assert report["full_regular_session_observed"] is False
        assert report["gate3"]["shadow_decisions"]["WOULD_SUBMIT"] == 1
        assert sum(report["gate3"]["shadow_decisions"].values()) == 1
        assert report["gate3"]["production_database_access"] is False
        assert card.entry_client_order_id == ""
        assert observer.journal.audit().passed
    finally:
        observer.shadow.close()


def test_gate4_passive_capture_works_with_formal_collection_disabled(monkeypatch):
    from gate4 import runtime_observer
    captured = []
    monkeypatch.setattr(runtime_observer, "configured_collector", lambda: None)
    monkeypatch.setattr(checks, "observe_execution_event", lambda kind, payload: captured.append((kind, payload)))
    runtime_observer.observe_gate4_event("MUTATION_TERMINAL", status="FILLED")
    assert captured == [("MUTATION_TERMINAL", {"status": "FILLED"})]
