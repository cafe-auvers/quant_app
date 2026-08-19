from pathlib import Path

import pytest

from src.core.order_state import OrderIntent, OrderSide, OrderStatus
from src.risk.pre_trade import PreTradeRiskDecision, PreTradeRiskRejectedError
from src.services import event_journal, order_execution_service, trading_state
from src.services.broker import BrokerSubmissionResult
from src.services.order_ledger import load_order_ledger
from src.services.order_execution_service import submit_guarded_overseas_order
from src.services.trading_state import TradingDisabledError


def test_event_journal_masks_accounts_and_recursively_redacts_secrets(tmp_path):
    path = tmp_path / "events.jsonl"

    written = event_journal.append_event(
        event_journal.EventType.SIGNAL_CREATED,
        symbol="nvda",
        account_no="12345678-01",
        order_id="PROD-12345678-01-NVDA-ORDER",
        quantity=147,
        reason=(
            "account=12345678-01 Authorization: Bearer top-secret-token "
            "access_token=query-secret"
        ),
        payload={
            "window": "5m",
            "access_token": "never-write-this",
            "nested": {"app_secret": "also-secret", "score": 91.2},
            "message": "password=hunter2 for 12345678-01",
        },
        path=path,
    )

    raw = path.read_text(encoding="utf-8")
    assert "12345678" not in raw
    assert "never-write-this" not in raw
    assert "also-secret" not in raw
    assert "top-secret-token" not in raw
    assert "query-secret" not in raw
    assert "hunter2" not in raw
    assert written["account"].startswith("12")
    assert written["account"].endswith("01")
    assert written["payload"]["access_token"] == "[REDACTED]"
    assert written["payload"]["nested"]["app_secret"] == "[REDACTED]"
    assert "12345678" not in written["order_id"]
    assert "[REDACTED]" in written["reason"]


def test_recent_events_are_newest_first_and_skip_torn_records(tmp_path):
    path = tmp_path / "events.jsonl"
    event_journal.append_event("FIRST", symbol="AAPL", path=path)
    event_journal.append_event("SECOND", symbol="NVDA", path=path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event_type":')

    assert [
        event["event_type"] for event in event_journal.load_recent_events(path=path)
    ] == ["SECOND", "FIRST"]
    assert [
        event["event_type"]
        for event in event_journal.load_recent_events(path=path, symbol="aapl")
    ] == ["FIRST"]


def test_journal_status_reports_last_write_sizes_archives_and_free_space(tmp_path):
    path = tmp_path / "events.jsonl"
    written = event_journal.append_event("ORDER_ACCEPTED", path=path)

    status = event_journal.inspect_event_journal(path)

    assert status.last_write_at == written["timestamp"]
    assert status.last_error == ""
    assert status.latest_event_at == written["timestamp"]
    assert status.active_file_size > 0
    assert status.archive_count == 0
    assert status.available_disk_space > 0
    assert status.directory_writable is True


def test_rotation_preserves_prior_journal_as_an_archive(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_journal, "MAX_JOURNAL_BYTES", 1)

    event_journal.append_event("FIRST", path=path)
    event_journal.append_event("SECOND", path=path)

    assert [
        event["event_type"] for event in event_journal.load_recent_events(path=path)
    ] == [
        "SECOND",
        "FIRST",
    ]
    assert len(list(tmp_path.glob("events.*.jsonl"))) == 1


def test_rotation_retains_only_the_newest_configured_archives(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_journal, "MAX_JOURNAL_BYTES", 1)
    monkeypatch.setattr(event_journal, "MAX_JOURNAL_ARCHIVES", 2)

    for event_type in ("FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH"):
        event_journal.append_event(event_type, path=path)

    assert len(list(tmp_path.glob("events.*.jsonl"))) == 2
    assert [
        event["event_type"]
        for event in event_journal.load_recent_events(path=path, limit=10)
    ] == ["FIFTH", "FOURTH", "THIRD"]


def test_retention_never_prunes_archives_before_new_active_write_succeeds(
    monkeypatch, tmp_path
):
    path = tmp_path / "events.jsonl"
    # Reproduce Windows' coarse wall-clock resolution deterministically: every
    # rotation must still receive a distinct archive name.
    monkeypatch.setattr(event_journal.time, "time_ns", lambda: 1234)
    monkeypatch.setattr(event_journal, "MAX_JOURNAL_BYTES", 1)
    monkeypatch.setattr(event_journal, "MAX_JOURNAL_ARCHIVES", 10)
    for event_type in ("FIRST", "SECOND", "THIRD"):
        event_journal.append_event(event_type, path=path)
    assert len(list(tmp_path.glob("events.*.jsonl"))) == 2

    monkeypatch.setattr(event_journal, "MAX_JOURNAL_ARCHIVES", 1)
    real_open = event_journal.os.open

    def fail_new_active_file(target, flags, *args):
        if Path(target) == path and flags & event_journal.os.O_APPEND:
            raise OSError("active journal unavailable")
        return real_open(target, flags, *args)

    monkeypatch.setattr(event_journal.os, "open", fail_new_active_file)
    with pytest.raises(OSError, match="active journal unavailable"):
        event_journal.append_event("FOURTH", path=path)

    assert len(list(tmp_path.glob("events.*.jsonl"))) == 3


def test_record_event_is_best_effort(monkeypatch):
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    assert event_journal.record_event("ORDER_ACCEPTED") is False
    status = event_journal.inspect_event_journal()
    assert "disk unavailable" in status.last_error
    assert status.last_error_at


def test_execution_succeeds_when_injected_event_recorder_fails(
    trading_enabled, tmp_path
):
    class FakeBroker:
        def submit_order(self, **_kwargs):
            return BrokerSubmissionResult("BROKER-1", {"accepted": True})

        def is_ambiguous_submission_error(self, _error):
            return False

    def failing_recorder(*_args, **_kwargs):
        raise OSError("journal unavailable")

    decision = PreTradeRiskDecision.approve(
        environment="SIM",
        account_no="12345678",
        symbol="NVDA",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=147,
        reference_price=100.0,
        exchange="NASD",
        execution_policy="REGULAR_LIMIT",
        strategy_id="ORB",
        plan_id="ORB:NVDA",
    )
    order = submit_guarded_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="NVDA",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=147,
        limit_price=100.0,
        path=tmp_path / "orders.json",
        broker=FakeBroker(),
        pre_trade_risk_decision=decision,
        strategy_id="ORB",
        plan_id="ORB:NVDA",
        event_recorder=failing_recorder,
    )

    assert order.status == OrderStatus.ACCEPTED
    assert order.broker_order_id == "BROKER-1"


def test_execution_emits_correlated_lifecycle_in_durable_order(
    tmp_path, trading_enabled
):
    class FakeBroker:
        def submit_order(self, **_kwargs):
            return BrokerSubmissionResult("BROKER-2", {"accepted": True})

        def is_ambiguous_submission_error(self, _error):
            return False

    events = []

    def capture(event_type, **fields):
        events.append((str(getattr(event_type, "value", event_type)), fields))
        return True

    decision = PreTradeRiskDecision.approve(
        environment="SIM",
        account_no="12345678",
        symbol="NVDA",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=147,
        reference_price=100.0,
        exchange="NASD",
        execution_policy="REGULAR_LIMIT",
        strategy_id="ORB",
        plan_id="ORB:NVDA",
    )
    order = submit_guarded_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="NVDA",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=147,
        limit_price=100.0,
        path=tmp_path / "orders.json",
        broker=FakeBroker(),
        pre_trade_risk_decision=decision,
        strategy_id="ORB",
        plan_id="ORB:NVDA",
        event_recorder=capture,
        signal_payload={"window": "5m", "stop_loss": 97.0, "score": 91.2},
    )

    assert [name for name, _fields in events] == [
        "SIGNAL_CREATED",
        "RISK_APPROVED",
        "ORDER_INTENT_CREATED",
        "ORDER_RESERVED",
        "ORDER_SUBMISSION_STARTED",
        "ORDER_ACCEPTED",
    ]
    assert all(fields["order_id"] == order.client_order_id for _, fields in events)
    assert all(fields["signal_id"] == "ORB:NVDA" for _, fields in events)


def test_submission_rechecks_kill_switch_after_started_journal_event(
    tmp_path, trading_enabled
):
    class FakeBroker:
        calls = 0

        def submit_order(self, **_kwargs):
            self.calls += 1
            return BrokerSubmissionResult("MUST-NOT-SUBMIT", {})

        def is_ambiguous_submission_error(self, _error):
            return False

    broker = FakeBroker()

    def recorder(event_type, **_fields):
        if event_type == "ORDER_SUBMISSION_STARTED":
            trading_state.set_trading_enabled(False)
        return True

    with pytest.raises(TradingDisabledError):
        submit_guarded_overseas_order(
            environment="SIM",
            account_no="12345678",
            symbol="NVDA",
            side=OrderSide.SELL,
            intent=OrderIntent.MANUAL_EXIT,
            quantity=1,
            limit_price=100.0,
            path=tmp_path / "orders.json",
            broker=broker,
            event_recorder=recorder,
        )

    assert broker.calls == 0
    [order] = load_order_ledger(tmp_path / "orders.json")
    assert order.status == OrderStatus.REJECTED


def test_submission_rechecks_risk_after_started_journal_event(
    monkeypatch, tmp_path, trading_enabled
):
    class FakeBroker:
        calls = 0

        def submit_order(self, **_kwargs):
            self.calls += 1
            return BrokerSubmissionResult("MUST-NOT-SUBMIT", {})

        def is_ambiguous_submission_error(self, _error):
            return False

    checks = 0

    def approval_expires_on_final_check(*_args, **_kwargs):
        nonlocal checks
        checks += 1
        if checks == 3:
            raise PreTradeRiskRejectedError("Pre-trade risk approval has expired")

    monkeypatch.setattr(
        order_execution_service,
        "require_pre_trade_risk_approval",
        approval_expires_on_final_check,
    )
    broker = FakeBroker()
    decision = PreTradeRiskDecision.approve(
        environment="SIM",
        account_no="12345678",
        symbol="NVDA",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=1,
        reference_price=100.0,
        exchange="NASD",
        execution_policy="REGULAR_LIMIT",
        strategy_id="ORB",
        plan_id="ORB:NVDA",
    )

    with pytest.raises(PreTradeRiskRejectedError):
        submit_guarded_overseas_order(
            environment="SIM",
            account_no="12345678",
            symbol="NVDA",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=1,
            limit_price=100.0,
            path=tmp_path / "orders.json",
            broker=broker,
            pre_trade_risk_decision=decision,
            strategy_id="ORB",
            plan_id="ORB:NVDA",
            event_recorder=lambda *_args, **_kwargs: True,
        )

    assert checks == 3
    assert broker.calls == 0
    [order] = load_order_ledger(tmp_path / "orders.json")
    assert order.status == OrderStatus.REJECTED


def test_risk_rejection_does_not_relabel_valid_strategy_signal(
    tmp_path, trading_enabled
):
    events = []
    decision = PreTradeRiskDecision.reject(
        reasons=("Position risk exceeds limit",),
        environment="SIM",
        account_no="12345678",
        symbol="NVDA",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=147,
        reference_price=100.0,
        exchange="NASD",
        execution_policy="REGULAR_LIMIT",
        strategy_id="ORB",
        plan_id="ORB:NVDA",
    )

    with pytest.raises(PreTradeRiskRejectedError):
        submit_guarded_overseas_order(
            environment="SIM",
            account_no="12345678",
            symbol="NVDA",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=147,
            limit_price=100.0,
            path=tmp_path / "orders.json",
            broker=object(),
            pre_trade_risk_decision=decision,
            strategy_id="ORB",
            plan_id="ORB:NVDA",
            event_recorder=lambda event, **_fields: events.append(event) or True,
            signal_payload={"window": "5m", "entry_trigger": 100.0},
        )

    assert events == ["SIGNAL_CREATED", "RISK_REJECTED"]
    assert not (tmp_path / "orders.json").exists()


def test_explicit_strategy_rejection_emits_signal_rejected(tmp_path, trading_enabled):
    events = []
    decision = PreTradeRiskDecision.reject(
        reasons=("No actionable breakout",),
        environment="SIM",
        account_no="12345678",
        symbol="NVDA",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=1,
        reference_price=100.0,
        exchange="NASD",
        execution_policy="REGULAR_LIMIT",
        strategy_id="ORB",
        plan_id="ORB:NVDA",
    )

    with pytest.raises(PreTradeRiskRejectedError):
        submit_guarded_overseas_order(
            environment="SIM",
            account_no="12345678",
            symbol="NVDA",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=1,
            limit_price=100.0,
            path=tmp_path / "orders.json",
            broker=object(),
            pre_trade_risk_decision=decision,
            strategy_id="ORB",
            plan_id="ORB:NVDA",
            event_recorder=lambda event, **_fields: events.append(event) or True,
            signal_payload={"reason": "No actionable breakout"},
            signal_event_type="SIGNAL_REJECTED",
        )

    assert events == ["SIGNAL_REJECTED", "RISK_REJECTED"]
