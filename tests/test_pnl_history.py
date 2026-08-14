import datetime as dt
from zoneinfo import ZoneInfo

from src.core.order_state import BrokerOrder, OrderSide, OrderStatus
from src.services import pnl_history

US_MARKET_ZONE = ZoneInfo("America/New_York")


def _filled_order(
    *,
    symbol="NVDA",
    side=OrderSide.BUY,
    quantity=10,
    price=100.0,
    day="2026-08-10",
    hour=10,
    environment="PROD",
    account_no="123",
):
    order = BrokerOrder.create(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        side=side,
        quantity_requested=quantity,
        limit_price=price,
    )
    order.status = OrderStatus.FILLED
    order.filled_quantity = quantity
    order.avg_fill_price = price
    timestamp = dt.datetime.fromisoformat(day).replace(
        hour=hour, tzinfo=US_MARKET_ZONE
    )
    order.updated_at = timestamp.isoformat()
    order.submitted_at = timestamp.isoformat()
    return order


def test_realized_pnl_fifo_matches_oldest_lot_first():
    orders = [
        _filled_order(side=OrderSide.BUY, quantity=10, price=100.0, day="2026-08-10", hour=10),
        _filled_order(side=OrderSide.BUY, quantity=10, price=110.0, day="2026-08-10", hour=11),
        _filled_order(side=OrderSide.SELL, quantity=15, price=120.0, day="2026-08-11", hour=10),
    ]
    daily = pnl_history.compute_realized_pnl_by_date(orders)
    # 10 sh from the $100 lot + 5 sh from the $110 lot, both sold at $120.
    assert daily == {"2026-08-11": 10 * (120 - 100) + 5 * (120 - 110)}


def test_realized_pnl_ignores_non_prod_orders():
    orders = [
        _filled_order(side=OrderSide.BUY, quantity=10, price=100.0, environment="PAPER"),
        _filled_order(side=OrderSide.SELL, quantity=10, price=150.0, environment="PAPER", day="2026-08-11"),
    ]
    assert pnl_history.compute_realized_pnl_by_date(orders) == {}


def test_realized_pnl_only_counts_the_matched_portion_of_an_oversized_sell():
    orders = [
        _filled_order(side=OrderSide.BUY, quantity=10, price=100.0, day="2026-08-10"),
        # Sells 20 shares but only 10 have a known cost basis (e.g. position
        # predates the ledger) -> only those 10 can be realized.
        _filled_order(side=OrderSide.SELL, quantity=20, price=120.0, day="2026-08-11"),
    ]
    daily = pnl_history.compute_realized_pnl_by_date(orders)
    assert daily == {"2026-08-11": 10 * (120 - 100)}


def test_realized_pnl_keeps_symbols_and_accounts_separate():
    orders = [
        _filled_order(symbol="NVDA", account_no="A", side=OrderSide.BUY, quantity=10, price=100.0, day="2026-08-10"),
        _filled_order(symbol="AAPL", account_no="A", side=OrderSide.BUY, quantity=10, price=50.0, day="2026-08-10"),
        # Selling AAPL must not consume the NVDA lot.
        _filled_order(symbol="AAPL", account_no="A", side=OrderSide.SELL, quantity=10, price=60.0, day="2026-08-11"),
    ]
    daily = pnl_history.compute_realized_pnl_by_date(orders)
    assert daily == {"2026-08-11": 10 * (60 - 50)}


def test_compute_unrealized_pnl_usd_skips_positions_without_a_live_price():
    positions = [
        {"symbol": "NVDA", "shares_held": 10, "avg_cost": 100.0},
        {"symbol": "AAPL", "shares_held": 5, "avg_cost": 50.0},
    ]
    prices = {"NVDA": 120.0}  # AAPL has no live price yet
    total = pnl_history.compute_unrealized_pnl_usd(positions, prices)
    assert total == 10 * (120.0 - 100.0)


def test_build_pnl_history_backfills_realized_and_isolates_unrealized_to_today():
    orders = [
        _filled_order(side=OrderSide.BUY, quantity=10, price=100.0, day="2026-08-10"),
        _filled_order(side=OrderSide.SELL, quantity=10, price=120.0, day="2026-08-11"),
    ]
    snapshots = pnl_history.build_pnl_history(
        orders,
        today="2026-08-12",
        unrealized_usd_today=50.0,
        fx_rate_today=1350.0,
        capital_base_usd_today=10_000.0,
    )
    by_date = {snap.date: snap for snap in snapshots}
    assert set(by_date) == {"2026-08-11", "2026-08-12"}

    day1 = by_date["2026-08-11"]
    assert day1.realized_usd == 200.0
    assert day1.unrealized_usd == 0.0
    assert day1.total_usd == 200.0
    assert day1.fx_rate is None  # no live FX known before "today"
    assert day1.capital_base_usd is None

    today = by_date["2026-08-12"]
    assert today.realized_usd == 200.0  # cumulative, no new fills today
    assert today.unrealized_usd == 50.0
    assert today.total_usd == 250.0
    assert today.fx_rate == 1350.0
    assert today.capital_base_usd == 10_000.0


def test_build_pnl_history_freezes_past_days_and_forward_fills_next_refresh():
    orders = [
        _filled_order(side=OrderSide.BUY, quantity=10, price=100.0, day="2026-08-10"),
        _filled_order(side=OrderSide.SELL, quantity=10, price=120.0, day="2026-08-11"),
    ]
    day1_snapshots = pnl_history.build_pnl_history(
        orders,
        today="2026-08-11",
        unrealized_usd_today=0.0,
        fx_rate_today=1300.0,
        capital_base_usd_today=9_000.0,
    )

    # Next day: no new live FX/capital-base yet (worker hasn't fetched them),
    # and a new BUY has been placed but nothing sold.
    orders_day2 = orders + [
        _filled_order(side=OrderSide.BUY, quantity=5, price=200.0, day="2026-08-12"),
    ]
    day2_snapshots = pnl_history.build_pnl_history(
        orders_day2,
        today="2026-08-12",
        unrealized_usd_today=25.0,
        fx_rate_today=None,
        capital_base_usd_today=None,
        existing=day1_snapshots,
    )
    by_date = {snap.date: snap for snap in day2_snapshots}

    # The frozen historical day keeps its own recorded FX/capital-base.
    assert by_date["2026-08-11"].fx_rate == 1300.0
    assert by_date["2026-08-11"].capital_base_usd == 9_000.0

    # Today forward-fills from the last known values when live ones are missing.
    today = by_date["2026-08-12"]
    assert today.realized_usd == 200.0  # the new BUY alone doesn't realize anything
    assert today.unrealized_usd == 25.0
    assert today.fx_rate == 1300.0
    assert today.capital_base_usd == 9_000.0


def test_record_daily_pnl_snapshot_round_trips_through_disk(tmp_path):
    path = tmp_path / "pnl_history.json"
    orders = [
        _filled_order(side=OrderSide.BUY, quantity=10, price=100.0, day="2026-08-10"),
        _filled_order(side=OrderSide.SELL, quantity=10, price=130.0, day="2026-08-11"),
    ]
    pnl_history.record_daily_pnl_snapshot(
        orders,
        unrealized_usd_today=0.0,
        fx_rate_today=1400.0,
        capital_base_usd_today=5_000.0,
        today="2026-08-11",
        path=path,
    )
    reloaded = pnl_history.load_pnl_history(path)
    assert len(reloaded) == 1
    assert reloaded[0].date == "2026-08-11"
    assert reloaded[0].realized_usd == 300.0
    assert reloaded[0].fx_rate == 1400.0

    # A later call with new fills must recompute the realized curve fresh
    # while keeping the earlier day's own recorded FX rate.
    orders_more = orders + [
        _filled_order(side=OrderSide.BUY, quantity=10, price=100.0, day="2026-08-12"),
        _filled_order(side=OrderSide.SELL, quantity=10, price=90.0, day="2026-08-13"),
    ]
    updated = pnl_history.record_daily_pnl_snapshot(
        orders_more,
        unrealized_usd_today=0.0,
        fx_rate_today=1420.0,
        capital_base_usd_today=5_200.0,
        today="2026-08-13",
        path=path,
    )
    by_date = {snap.date: snap for snap in updated}
    assert by_date["2026-08-11"].fx_rate == 1400.0
    assert by_date["2026-08-13"].realized_usd == 300.0 + 10 * (90 - 100)
