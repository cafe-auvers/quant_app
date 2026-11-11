"""Supported exit sessions never authorize ORB entries or overnight trading."""
from datetime import datetime, timezone

import pytest

from src.core.exit_execution_command import build_exit_execution_command
from src.core.order_state import OrderIntent
from src.services.kis_realtime_market_data import PendingMarketState, StopRule
from src.services.realtime_market_data import QuoteSnapshot
from src.utils.market_calendar import is_regular_session_open, is_us_exit_session_open


@pytest.mark.parametrize("stamp,expected", [
    ("2026-11-11T08:59:59+00:00", False),
    ("2026-11-11T09:00:00+00:00", True),
    ("2026-11-11T13:30:00+00:00", True),
    ("2026-11-11T21:59:59+00:00", True),
    ("2026-11-11T22:00:00+00:00", False),
    ("2026-08-17T21:59:59+00:00", True),
    ("2026-08-17T22:00:00+00:00", False),
    ("2026-11-26T13:30:00+00:00", False),
    ("2026-11-08T13:30:00+00:00", False),
])
def test_kis_default_exit_window_respects_dst_holidays_and_close(stamp, expected):
    assert is_us_exit_session_open(datetime.fromisoformat(stamp)) is expected


@pytest.mark.parametrize("intent", [OrderIntent.MANUAL_EXIT, OrderIntent.PARTIAL_EXIT, OrderIntent.STOP_LOSS])
def test_extended_hours_use_live_limit_endpoint_instead_of_opening_reservation(intent):
    command = build_exit_execution_command(
        environment="PROD", account_no="1", symbol="OMH", intent=intent,
        quantity=150, regular_session_open=False, exit_session_open=True, limit_price=27.2,
    )
    assert command.execution_policy == "REGULAR_LIMIT"
    assert command.quantity == 150
    assert command.limit_price == 27.2


def test_premarket_breach_is_latched_even_if_price_recovers_before_drain():
    stamp = datetime(2026, 11, 11, 13, 30, tzinfo=timezone.utc)
    assert not is_regular_session_open(stamp)
    pending = PendingMarketState()
    rule = StopRule("PROD:1:OMH", 28, "v1")
    for price in (29, 27.35, 29):
        pending.add_trade(QuoteSnapshot(symbol="OMH", last_price=price,
            received_at=stamp, broker_event_at=stamp, regular_session=False), {rule.card_key:rule})
    assert pending.stop_breach_latched
    assert pending.minimum_trade.last_price == 27.35
    assert pending.breached_stop_versions == {(rule.card_key, "v1")}


def test_closed_session_does_not_latch_protective_breach():
    stamp = datetime(2026, 11, 11, 23, tzinfo=timezone.utc)
    pending = PendingMarketState()
    rule = StopRule("PROD:1:OMH", 28, "v1")
    pending.add_trade(QuoteSnapshot(symbol="OMH", last_price=27,
        received_at=stamp, regular_session=False), {rule.card_key:rule})
    assert not pending.stop_breach_latched
