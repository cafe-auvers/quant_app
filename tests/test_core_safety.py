import datetime as dt

import pandas as pd
import pytest

from src.core.orb import (
    calculate_orb_range,
    evaluate_orb_entry_signal,
    resample_intraday_bars,
)
from src.core.order_state import (
    BrokerOrder,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
    generate_client_order_id,
)
from src.risk.position_sizer import PositionSizer
from src.core.scoring import calculate_deterministic_scores
from src.core.trade_reviewer import TradeReviewer, TradeSetup
from src.services.order_reconciliation import (
    reconcile_order_with_broker_snapshot,
    reconcile_orders_with_snapshot,
)


def _history() -> pd.DataFrame:
    closes = [100.0 + index for index in range(30)]
    return pd.DataFrame(
        {
            "Open": [price * 0.99 for price in closes],
            "High": [price * 1.02 for price in closes],
            "Low": [price * 0.98 for price in closes],
            "Close": closes,
            "Volume": [50_000.0] * len(closes),
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def _order(
    *,
    quantity: int = 10,
    side: OrderSide = OrderSide.BUY,
    status: OrderStatus = OrderStatus.ACCEPTED,
) -> BrokerOrder:
    return BrokerOrder.create(
        environment="SIM",
        account_no="12345678-01",
        symbol="AAPL",
        side=side,
        intent=OrderIntent.ENTRY,
        quantity_requested=quantity,
        limit_price=100.0,
        status=status,
    )


def _holdings_snapshot(quantity: int, average_price: float = 100.0) -> dict:
    holdings = (
        [
            {
                "symbol": "AAPL",
                "quantity": quantity,
                "average_price": average_price,
            }
        ]
        if quantity
        else []
    )
    return {"domestic": {"holdings": []}, "overseas": {"holdings": holdings}}


@pytest.mark.parametrize("account_size", [0, -1, float("nan"), float("inf")])
def test_position_sizer_fails_closed_for_invalid_account_size(account_size):
    size = PositionSizer(account_size=account_size).size_risk_based(100.0, 95.0)

    assert size.shares == 0
    assert size.dollar_amount == 0.0
    assert size.risk_amount == 0.0


@pytest.mark.parametrize("stop_loss", [100.0, 101.0, 0.0, float("nan"), float("inf")])
def test_position_sizer_fails_closed_for_invalid_long_stop(stop_loss):
    size = PositionSizer(account_size=10_000.0).size_risk_based(100.0, stop_loss)

    assert size.shares == 0
    assert size.dollar_amount == 0.0


def test_scoring_rejects_invalid_stop_and_account_before_sizing():
    invalid_stop = calculate_deterministic_scores(
        "AAPL",
        _history(),
        entry_price=130.0,
        stop_loss=131.0,
        account_size=100_000.0,
        risk_percent=0.01,
    )
    invalid_account = calculate_deterministic_scores(
        "AAPL",
        _history(),
        entry_price=130.0,
        stop_loss=120.0,
        account_size=float("nan"),
        risk_percent=0.01,
    )

    assert invalid_stop["status"] == "REJECTED"
    assert invalid_stop["shares"] == 0
    assert "below entry price" in invalid_stop["warnings"][0]
    assert invalid_account["status"] == "REJECTED"
    assert invalid_account["shares"] == 0
    assert "Account size" in invalid_account["warnings"][0]


def test_trade_reviewer_uses_rules_when_optional_ai_adapter_is_unimplemented(tmp_path):
    (tmp_path / "rules.md").write_text("# Risk rules", encoding="utf-8")
    reviewer = TradeReviewer(rulebook_dir=tmp_path)

    review = reviewer.review_trade(
        TradeSetup(
            symbol="AAPL",
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=0.0,
            size_shares=10,
            risk_amount=50.0,
            reasoning="Breakout from a tight base",
        )
    )

    assert review.approved is True
    assert review.violations == []


@pytest.mark.parametrize("buffer_pct", [-0.001, float("nan"), float("inf")])
def test_orb_rejects_non_finite_or_negative_breakout_buffer(buffer_pct):
    signal = evaluate_orb_entry_signal(
        orb_high=103.0,
        orb_low=95.0,
        breakout_price=102.0,
        current_price=104.0,
        buffer_pct=buffer_pct,
    )

    assert signal.signal == "invalid_buffer_pct"
    assert signal.allow_entry is False
    assert signal.allow_full_size is False
    assert signal.suggested_size_multiplier == 0.0


def test_orb_range_uses_latest_market_session_not_oldest_cached_session():
    old_index = pd.date_range("2026-01-05 09:30", periods=6, freq="min")
    current_index = pd.date_range("2026-01-06 09:30", periods=6, freq="min")
    old_session = pd.DataFrame(
        {
            "High": [110, 111, 112, 113, 114, 115],
            "Low": [90, 91, 92, 93, 94, 95],
        },
        index=old_index,
    )
    current_session = pd.DataFrame(
        {
            "High": [11, 12, 13, 14, 15, 16],
            "Low": [9, 10, 11, 12, 13, 14],
        },
        index=current_index,
    )

    orb_range = calculate_orb_range("AAPL", pd.concat([old_session, current_session]), "5m")

    assert orb_range is not None
    assert orb_range.start.date() == dt.date(2026, 1, 6)
    assert orb_range.high == 15.0
    assert orb_range.low == 9.0


def test_orb_range_converts_utc_bars_to_us_market_open():
    index = pd.date_range("2026-01-06 14:30", periods=6, freq="min", tz="UTC")
    bars = pd.DataFrame(
        {
            "High": [11, 12, 13, 14, 15, 16],
            "Low": [9, 10, 11, 12, 13, 14],
        },
        index=index,
    )

    orb_range = calculate_orb_range("AAPL", bars, "5m")

    assert orb_range is not None
    assert orb_range.start.hour == 9
    assert orb_range.start.minute == 30
    assert str(orb_range.start.tz) == "America/New_York"


def test_orb_range_recognizes_naive_utc_bars_from_the_local_cache():
    # db_loader persists aware provider timestamps in UTC after stripping tz.
    index = pd.date_range("2026-01-06 14:30", periods=6, freq="min")
    bars = pd.DataFrame(
        {
            "High": [11, 12, 13, 14, 15, 16],
            "Low": [9, 10, 11, 12, 13, 14],
        },
        index=index,
    )

    orb_range = calculate_orb_range("AAPL", bars, "5m")

    assert orb_range is not None
    assert orb_range.start.hour == 9
    assert orb_range.start.minute == 30
    assert str(orb_range.start.tz) == "America/New_York"


def test_hourly_resample_is_anchored_to_market_open_for_orb():
    index = pd.date_range("2026-01-06 09:30", periods=61, freq="min")
    bars = pd.DataFrame(
        {
            "Open": [100.0 + index for index in range(61)],
            "High": [101.0 + index for index in range(61)],
            "Low": [99.0 + index for index in range(61)],
            "Close": [100.5 + index for index in range(61)],
            "Volume": [100.0] * 61,
        },
        index=index,
    )

    hourly = resample_intraday_bars(bars, "1h")
    orb_range = calculate_orb_range("AAPL", hourly, "1h")

    assert hourly.index[0].hour == 9
    assert hourly.index[0].minute == 30
    assert orb_range is not None
    assert orb_range.high == 160.0
    assert orb_range.low == 99.0


def test_client_order_ids_remain_unique_for_same_timestamp_burst():
    timestamp = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    client_order_ids = {
        generate_client_order_id(
            "SIM",
            "12345678-01",
            "AAPL",
            OrderSide.BUY,
            OrderIntent.ENTRY,
            timestamp=timestamp,
        )
        for _ in range(500)
    }

    assert len(client_order_ids) == 500


def test_holdings_delta_is_allocated_once_across_same_symbol_orders():
    first = _order(quantity=10)
    second = _order(quantity=10)
    first.submitted_at = "2026-01-01T00:00:00+00:00"
    second.submitted_at = "2026-01-01T00:00:01+00:00"

    updated_first, updated_second = reconcile_orders_with_snapshot(
        [first, second],
        snapshot=_holdings_snapshot(10),
        previous_snapshot=_holdings_snapshot(0),
    )

    assert updated_first.status == OrderStatus.FILLED
    assert updated_first.filled_quantity == 10
    assert updated_second.status == OrderStatus.WORKING
    assert updated_second.filled_quantity == 0
    assert updated_first.filled_quantity + updated_second.filled_quantity == 10


def test_terminal_cancel_preserves_known_partial_fill_for_later_application():
    order = _order(status=OrderStatus.PARTIALLY_FILLED)
    order.filled_quantity = 4
    order.remaining_quantity = 6
    snapshot = BrokerOrderStatusSnapshot(
        environment="SIM",
        account_no="12345678-01",
        symbol="AAPL",
        side=OrderSide.BUY,
        status=OrderStatus.CANCELLED,
        quantity_requested=10,
        filled_quantity=0,
        remaining_quantity=0,
    )

    updated = reconcile_order_with_broker_snapshot(order, snapshot)

    assert updated.status == OrderStatus.CANCELLED
    assert updated.filled_quantity == 4
    assert updated.remaining_quantity == 6
