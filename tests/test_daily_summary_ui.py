from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QWidget

from src.core.exit_policy import market_session_date
from src.services.daily_trading_summary import (
    DailyPlanItem,
    DailyRejectedOrbCombination,
    DailyTradingSummary,
    PLAN_ORIGIN_ADDED_INTRADAY,
    PLAN_ORIGIN_TODAYS_PLAN,
)
from src.ui.health.panel import HealthPanelMixin


_APP = None


class _DailySummaryHarness(QWidget, HealthPanelMixin):
    def __init__(self) -> None:
        super().__init__()
        self.daily_summary_widget = QWidget(self)
        self._build_daily_summary_main_tab()


def _plan(symbol: str, account: str, origin: str) -> DailyPlanItem:
    return DailyPlanItem(
        source=(
            "PUBLISHED PLAN"
            if origin == PLAN_ORIGIN_TODAYS_PLAN
            else "ADDED LATER"
        ),
        symbol=symbol,
        account_no=account,
        breakout_price=100.0,
        planned_quantity=10,
        outcome="WAITING BREAKOUT",
        reason_category="BREAKOUT NOT REACHED",
        reason="Waiting for entry trigger",
        origin=origin,
        orb_detail_count=1,
    )


def _detail(symbol: str, account: str, origin: str) -> DailyRejectedOrbCombination:
    return DailyRejectedOrbCombination(
        symbol=symbol,
        risk_percent=0.005,
        window="5m",
        classification="VALID",
        status="WAITING_BREAKOUT",
        orb_high=101.0,
        breakout_price=100.0,
        breakout_trigger=100.1,
        entry_trigger=101.0,
        stop_price=98.0,
        shares=10,
        capital_percent=10.0,
        stop_adr=50.0,
        reason="Waiting for breakout",
        account_no=account,
        source=(
            "PUBLISHED PLAN"
            if origin == PLAN_ORIGIN_TODAYS_PLAN
            else "ADDED LATER"
        ),
        origin=origin,
    )


def test_daily_summary_origin_and_plan_filters_reduce_both_tables():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    widget = _DailySummaryHarness()
    summary = DailyTradingSummary(
        session_date=market_session_date(),
        published_at="08:00:00 ET",
        plan_items=(
            _plan("ATHM", "1111", PLAN_ORIGIN_TODAYS_PLAN),
            _plan("PAY", "2222", PLAN_ORIGIN_ADDED_INTRADAY),
        ),
        positions=(),
        activities=(),
        orb_details=(
            _detail("ATHM", "1111", PLAN_ORIGIN_TODAYS_PLAN),
            _detail("PAY", "2222", PLAN_ORIGIN_ADDED_INTRADAY),
        ),
    )

    widget._on_daily_summary_completed(summary)

    assert widget.health_daily_plan_table.horizontalHeaderItem(0).text() == "Plan origin"
    assert widget.health_daily_plan_table.rowCount() == 2
    assert widget.health_daily_orb_details_table.rowCount() == 2

    intraday_index = widget.health_daily_origin_filter.findData(
        PLAN_ORIGIN_ADDED_INTRADAY
    )
    widget.health_daily_origin_filter.setCurrentIndex(intraday_index)
    assert widget.health_daily_plan_table.rowCount() == 1
    assert widget.health_daily_plan_table.item(0, 2).text() == "PAY"
    assert widget.health_daily_orb_details_table.rowCount() == 1
    assert widget.health_daily_plan_filter.count() == 2

    widget.health_daily_origin_filter.setCurrentIndex(0)
    assert widget.health_daily_plan_table.rowCount() == 2
    assert widget.health_daily_plan_filter.count() == 3

    pay_key = widget._daily_plan_key("2222", "PAY")
    widget.health_daily_plan_filter.setCurrentIndex(
        widget.health_daily_plan_filter.findData(pay_key)
    )
    assert widget.health_daily_plan_table.rowCount() == 1
    assert widget.health_daily_orb_details_table.rowCount() == 1
    assert widget.health_daily_orb_details_table.item(0, 2).text() == "PAY"

    widget.health_daily_plan_filter.setCurrentIndex(0)
    widget._show_daily_plan_orb_details(0, 0)
    assert widget.health_daily_tabs.currentIndex() == 3
    assert widget.health_daily_plan_filter.currentData() == widget._daily_plan_key(
        "1111", "ATHM"
    )
    assert widget.health_daily_orb_details_table.rowCount() == 1
    widget.close()
