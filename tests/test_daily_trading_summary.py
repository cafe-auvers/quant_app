from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import create_engine, func, select

from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.order_state import OrderIntent, OrderSide
from src.core.execution_queue import (
    ExecutionQueueItem,
    OrbCandidate,
    OrbCandidateStatus,
)
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    TradeCardState,
)
from src.services.daily_trading_summary import (
    PLAN_ORIGIN_ADDED_INTRADAY,
    PLAN_ORIGIN_TODAYS_PLAN,
    PLAN_ORIGIN_UNKNOWN,
    build_daily_trading_summary,
    ensure_daily_trading_events_table,
    record_buy_today_added,
    record_plan_published,
    record_trade_card_snapshot,
)
from src.services import daily_trading_summary as daily_summary_module
from src.services.execution_order_repository import record_execution_order
from src.services.trade_card_orb_bridge import TradeCardOrbEvaluator


SESSION = date(2026, 8, 25)


def _engine():
    return create_engine("sqlite:///:memory:")


def _card(symbol: str, **overrides) -> TradeCardState:
    fields = dict(
        environment="PROD",
        account_no="1234",
        symbol=symbol,
        board_status=BoardStatus.BUY_TODAY,
        session_date=SESSION,
        breakout_price=100.0,
        planned_quantity=10,
        entry_runtime_status=EntryRuntimeStatus.ORB_FORMING,
    )
    fields.update(overrides)
    return TradeCardState(**fields)


def _filled_order(
    symbol: str,
    *,
    side: OrderSide,
    intent: OrderIntent,
    quantity: int,
    price: float,
    suffix: str,
) -> ExecutionOrderRecord:
    return ExecutionOrderRecord(
        environment="PROD",
        account_no="1234",
        symbol=symbol,
        side=side,
        intent=intent,
        client_order_id=f"CID-{suffix}",
        broker_order_id=f"BROKER-{suffix}",
        submitted_quantity=quantity,
        filled_quantity=quantity,
        remaining_quantity=0,
        average_fill_price=price,
        prepared_at="2026-08-25T14:31:00+00:00",
        market_session_date=SESSION.isoformat(),
        status=ExecutionOrderStatus.FILLED,
        broker_identity_status=BrokerIdentityStatus.EXACT,
    )


def test_summary_distinguishes_published_plan_from_later_addition_and_reasons():
    engine = _engine()
    published = _card("ATHM")
    added = _card("PAY", breakout_price=55.0)
    record_plan_published(
        engine,
        session_date=SESSION,
        cards=[published],
        revisions={"execution_queue": 7},
        occurred_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    record_buy_today_added(
        engine,
        added,
        command_id="ADD-PAY",
        occurred_at=datetime(2026, 8, 25, 12, 5, tzinfo=timezone.utc),
    )

    published.entry_runtime_status = EntryRuntimeStatus.WAITING_BREAKOUT
    published.entry_trigger = 101.25
    published.entry_block_reason = "Waiting for price to clear the entry trigger"
    record_trade_card_snapshot(
        engine,
        published,
        occurred_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )
    added.board_status = BoardStatus.BUYLIST
    added.session_date = None
    added.last_buy_today_session_date = SESSION
    added.entry_runtime_status = None
    added.buy_today_note = "Buy Today rejected - all ORB plans invalid."
    record_trade_card_snapshot(
        engine,
        added,
        occurred_at=datetime(2026, 8, 25, 20, 1, tzinfo=timezone.utc),
    )

    summary = build_daily_trading_summary(engine, SESSION, current_cards=[])

    assert [(row.source, row.symbol) for row in summary.plan_items] == [
        ("PUBLISHED PLAN", "ATHM"),
        ("ADDED LATER", "PAY"),
    ]
    assert [row.origin for row in summary.plan_items] == [
        PLAN_ORIGIN_TODAYS_PLAN,
        PLAN_ORIGIN_ADDED_INTRADAY,
    ]
    assert summary.plan_items[0].outcome == "WAITING BREAKOUT"
    assert summary.plan_items[0].reason_category == "BREAKOUT NOT REACHED"
    assert summary.plan_items[1].outcome == "ORB REJECTED"
    assert summary.plan_items[1].reason_category == "ORB NOT MET"


def test_summary_reconstructs_open_position_and_partial_and_full_sells():
    engine = _engine()
    for order in (
        _filled_order(
            "ATHM", side=OrderSide.BUY, intent=OrderIntent.ENTRY,
            quantity=10, price=100.0, suffix="ATHM-BUY",
        ),
        _filled_order(
            "ATHM", side=OrderSide.SELL, intent=OrderIntent.PARTIAL_EXIT,
            quantity=3, price=110.0, suffix="ATHM-PARTIAL",
        ),
        _filled_order(
            "PAY", side=OrderSide.BUY, intent=OrderIntent.ENTRY,
            quantity=5, price=50.0, suffix="PAY-BUY",
        ),
        _filled_order(
            "PAY", side=OrderSide.SELL, intent=OrderIntent.MANUAL_EXIT,
            quantity=5, price=52.0, suffix="PAY-FULL",
        ),
    ):
        record_execution_order(engine, order)

    summary = build_daily_trading_summary(engine, SESSION, current_cards=[])

    assert [(position.symbol, position.quantity) for position in summary.positions] == [
        ("ATHM", 7)
    ]
    labels = [activity.activity for activity in summary.activities]
    assert "PARTIAL SELL" in labels
    assert "FULL SELL" in labels


def test_identical_card_snapshot_is_idempotent():
    engine = _engine()
    card = _card("ATHM")
    occurred_at = datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc)
    record_trade_card_snapshot(engine, card, occurred_at=occurred_at)
    record_trade_card_snapshot(engine, card, occurred_at=occurred_at)

    table = ensure_daily_trading_events_table(engine)
    with engine.connect() as conn:
        count = conn.execute(select(func.count(table.c.id))).scalar_one()
    assert count == 1


def test_position_snapshot_is_logged_on_the_day_it_changed_not_entry_day():
    engine = _engine()
    card = _card(
        "ATHM",
        board_status=BoardStatus.OPEN_POSITION,
        session_date=date(2026, 8, 20),
        broker_quantity=8,
        average_entry_price=99.0,
        entry_runtime_status=None,
    )
    record_trade_card_snapshot(
        engine,
        card,
        occurred_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )

    old_day = build_daily_trading_summary(
        engine, date(2026, 8, 20), current_cards=[]
    )
    changed_day = build_daily_trading_summary(engine, SESSION, current_cards=[])

    assert old_day.positions == ()
    assert [(position.symbol, position.quantity) for position in changed_day.positions] == [
        ("ATHM", 8)
    ]


def test_daily_summary_keeps_all_rejected_orb_combinations_and_metrics():
    engine = _engine()
    card = _card("PAY", breakout_price=100.0)
    record_plan_published(
        engine,
        session_date=SESSION,
        cards=[card],
        revisions={"execution_queue": 9},
        occurred_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    candidates = {}
    for window in ("1m", "5m", "30m"):
        candidates[window] = OrbCandidate(
            symbol="PAY",
            window=window,
            orb_high=101.0,
            orb_low=98.0,
            breakout_price=100.0,
            breakout_trigger=100.1,
            entry_trigger=101.0,
            source_session_date=SESSION.isoformat(),
            stop_loss=98.0,
            shares=100,
            capital_percent=10.0,
            stop_loss_percent=2.97,
            stop_adr=50.0,
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            terminal_rejection=True,
            reason=f"{window} ORB high failed the plan",
        )
    queue_item = ExecutionQueueItem(
        symbol="PAY",
        environment="PROD",
        account_no="1234",
        breakout_price=100.0,
        candidates=candidates,
        last_updated=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )
    TradeCardOrbEvaluator(
        clock=lambda: datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
    ).update_card(card, queue_item)
    record_trade_card_snapshot(
        engine,
        card,
        occurred_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )

    summary = build_daily_trading_summary(engine, SESSION, current_cards=[])

    assert card.board_status == BoardStatus.BUYLIST
    assert len(summary.rejected_orb_combinations) == 24
    assert {
        (row.risk_percent, row.window)
        for row in summary.rejected_orb_combinations
    } == {
        (risk, window)
        for risk in (0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.0175, 0.02)
        for window in ("1m", "5m", "30m")
    }
    first = summary.rejected_orb_combinations[0]
    assert first.classification == "INVALID"
    assert first.orb_high == 101.0
    assert first.breakout_trigger == 100.1
    assert first.stop_price == 98.0
    assert "failed the plan" in first.reason


def test_republished_plan_keeps_symbols_from_earlier_verified_revision():
    engine = _engine()
    athm = _card("ATHM")
    pay = _card("PAY")
    record_plan_published(
        engine,
        session_date=SESSION,
        cards=[athm, pay],
        revisions={"execution_queue": 1},
        occurred_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    record_plan_published(
        engine,
        session_date=SESSION,
        cards=[athm],
        revisions={"execution_queue": 2},
        occurred_at=datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc),
    )

    summary = build_daily_trading_summary(engine, SESSION, current_cards=[])

    assert [item.symbol for item in summary.plan_items] == ["ATHM", "PAY"]
    assert all(item.source == "PUBLISHED PLAN" for item in summary.plan_items)
    assert all(
        item.origin == PLAN_ORIGIN_TODAYS_PLAN for item in summary.plan_items
    )


def test_intraday_addition_stays_intraday_when_live_plan_is_republished():
    engine = _engine()
    athm = _card("ATHM")
    pay = _card("PAY")
    record_plan_published(
        engine,
        session_date=SESSION,
        cards=[athm],
        revisions={"execution_queue": 1},
        occurred_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    record_buy_today_added(
        engine,
        pay,
        command_id="ADD-PAY-LIVE",
        occurred_at=datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc),
    )
    record_plan_published(
        engine,
        session_date=SESSION,
        cards=[athm, pay],
        revisions={"execution_queue": 2},
        occurred_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )

    summary = build_daily_trading_summary(engine, SESSION, current_cards=[])
    by_symbol = {item.symbol: item for item in summary.plan_items}

    assert by_symbol["ATHM"].origin == PLAN_ORIGIN_TODAYS_PLAN
    assert by_symbol["PAY"].origin == PLAN_ORIGIN_ADDED_INTRADAY
    assert by_symbol["PAY"].source == "ADDED LATER"


def test_legacy_orb_rejection_is_recovered_with_per_plan_details(monkeypatch):
    engine = _engine()
    card = _card(
        "EFOR",
        board_status=BoardStatus.BUYLIST,
        previous_board_status=BoardStatus.BUY_TODAY,
        board_status_updated_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
        session_date=None,
        entry_runtime_status=None,
        buy_today_note="Buy Today rejected - all ORB plans invalid.",
    )
    candidates = {
        window: OrbCandidate(
            symbol="EFOR",
            window=window,
            orb_high=31.0,
            orb_low=29.0,
            breakout_price=32.0,
            breakout_trigger=32.032,
            entry_trigger=31.0,
            source_session_date=SESSION.isoformat(),
            stop_loss=29.0,
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            terminal_rejection=True,
            reason=f"{window} ORB high did not clear breakout",
        )
        for window in ("1m", "5m", "30m")
    }
    queue_item = ExecutionQueueItem(
        symbol="EFOR",
        environment="PROD",
        account_no="1234",
        breakout_price=32.0,
        candidates=candidates,
        last_updated=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        daily_summary_module,
        "_queue_items_for_session",
        lambda _engine, _session: {("1234", "EFOR"): queue_item},
    )

    summary = build_daily_trading_summary(
        engine,
        SESSION,
        current_cards=[card],
    )

    assert [(item.symbol, item.outcome) for item in summary.plan_items] == [
        ("EFOR", "ORB REJECTED")
    ]
    assert summary.plan_items[0].source == "RECOVERED ORB REJECTION"
    assert summary.plan_items[0].origin == PLAN_ORIGIN_UNKNOWN
    assert summary.plan_items[0].orb_window == "1m / 5m / 30m"
    assert summary.plan_items[0].orb_detail_count == 24
    assert len(summary.orb_details) == 24
    assert len(summary.rejected_orb_combinations) == 24


def test_active_buy_today_plan_exposes_selected_orb_and_all_combinations(monkeypatch):
    engine = _engine()
    card = _card(
        "RNG",
        risk_percent=0.005,
        selected_orb_window="5m",
        entry_orb_window="5m",
        entry_orb_high=69.75,
        entry_orb_low=67.50,
        entry_trigger=69.75,
        stop_adr=65.0,
        entry_runtime_status=EntryRuntimeStatus.WAITING_BREAKOUT,
    )
    candidates = {
        window: OrbCandidate(
            symbol="RNG",
            window=window,
            orb_high=69.75,
            orb_low=67.50,
            breakout_price=69.0,
            breakout_trigger=69.069,
            entry_trigger=69.75,
            source_session_date=SESSION.isoformat(),
            stop_loss=67.50,
            shares=20,
            capital_percent=14.0,
            stop_loss_percent=3.23,
            stop_adr=65.0,
            status=OrbCandidateStatus.WAITING_BREAKOUT,
            valid=True,
            reason="Waiting for breakout",
        )
        for window in ("1m", "5m", "30m")
    }
    queue_item = ExecutionQueueItem(
        symbol="RNG",
        environment="PROD",
        account_no="1234",
        breakout_price=69.0,
        candidates=candidates,
        selected_window="5m",
        selected_candidate=candidates["5m"],
        last_updated=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        daily_summary_module,
        "_queue_items_for_session",
        lambda _engine, _session: {("1234", "RNG"): queue_item},
    )

    summary = build_daily_trading_summary(
        engine,
        SESSION,
        current_cards=[card],
    )

    plan = summary.plan_items[0]
    assert plan.symbol == "RNG"
    assert plan.orb_window == "5m"
    assert plan.orb_high == 69.75
    assert plan.orb_low == 67.50
    assert plan.entry_trigger == 69.75
    assert plan.orb_detail_count == 24
    selected = [detail for detail in summary.orb_details if detail.selected]
    assert [(detail.window, detail.risk_percent) for detail in selected] == [
        ("5m", 0.005)
    ]


def test_undated_legacy_fill_does_not_create_permanent_open_position():
    engine = _engine()
    order = _filled_order(
        "STIM",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=791,
        price=12.0,
        suffix="STIM-LEGACY",
    )
    order.market_session_date = None
    record_execution_order(engine, order)

    summary = build_daily_trading_summary(engine, SESSION, current_cards=[])

    assert summary.positions == ()
    assert [activity.symbol for activity in summary.activities] == ["STIM"]
