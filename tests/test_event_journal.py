from src.core.order_state import OrderIntent, OrderSide, OrderStatus
from src.risk.pre_trade import PreTradeRiskDecision
from src.services import event_journal
from src.services.broker import BrokerSubmissionResult
from src.services.order_execution_service import submit_guarded_overseas_order


def test_event_journal_masks_accounts_and_recursively_redacts_secrets(tmp_path):
    path = tmp_path / "events.jsonl"

    written = event_journal.append_event(
        event_journal.EventType.SIGNAL_CREATED,
        symbol="nvda",
        account_no="12345678-01",
        quantity=147,
        payload={
            "window": "5m",
            "access_token": "never-write-this",
            "nested": {"app_secret": "also-secret", "score": 91.2},
        },
        path=path,
    )

    raw = path.read_text(encoding="utf-8")
    assert "12345678" not in raw
    assert "never-write-this" not in raw
    assert "also-secret" not in raw
    assert written["account"].startswith("12")
    assert written["account"].endswith("01")
    assert written["payload"]["access_token"] == "[REDACTED]"
    assert written["payload"]["nested"]["app_secret"] == "[REDACTED]"


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


def test_record_event_is_best_effort(monkeypatch):
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    assert event_journal.record_event("ORDER_ACCEPTED") is False


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
