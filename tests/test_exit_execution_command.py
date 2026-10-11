"""Protective SELL pricing across separate trade and bid feeds."""
import pytest

from src.core.exit_execution_command import marketable_exit_limit_price


def test_fresh_trade_prices_stop_below_market_without_bid_feed():
    price = marketable_exit_limit_price(
        last_price=299.0,
        last_trusted_price=304.52,
        bid_price=305.0,
        quote_is_execution_ready=False,
        trade_is_execution_ready=True,
        hard_stop=True,
    )
    assert price == pytest.approx(297.505)
    assert price < 299.0


def test_fresh_last_trade_takes_precedence_over_old_trusted_price():
    assert marketable_exit_limit_price(
        last_price=70.0, last_trusted_price=100.0
    ) == pytest.approx(69.65)


def test_stale_trade_cannot_replace_trusted_stop_observation():
    assert marketable_exit_limit_price(
        last_price=150.0,
        last_trusted_price=70.0,
        quote_is_execution_ready=False,
        trade_is_execution_ready=False,
        hard_stop=True,
    ) == pytest.approx(69.65)


def test_hard_stop_keeps_repricing_below_previous_five_percent_floor():
    assert marketable_exit_limit_price(
        last_trusted_price=100.0,
        quote_is_execution_ready=False,
        emergency_reprice_attempt=20,
        hard_stop=True,
    ) == pytest.approx(89.5)


def test_hard_stop_repricing_has_a_valid_positive_minimum_tick():
    assert marketable_exit_limit_price(
        last_trusted_price=0.50,
        quote_is_execution_ready=False,
        emergency_reprice_attempt=1000,
        hard_stop=True,
    ) == pytest.approx(0.0001)
