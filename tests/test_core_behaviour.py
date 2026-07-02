from pathlib import Path
import datetime as dt

import pandas as pd
from sqlalchemy import MetaData, create_engine, insert

from src.core.position_sizer import PositionSizer
from src.core.orb import (
    calculate_orb_range,
    evaluate_orb_entry_signal,
    evaluate_orb_signal,
    OrbEntrySignal,
    resample_intraday_bars,
)
from src.core.scanner import StockScanner
from src.core.trade_reviewer import TradeReviewer, TradeSetup
from src.core.watchlist import TradePlan, TradePlanManager, Watchlist
import src.api.kis_account_snapshot_dual as kis_snapshot
from src.api.kis_account_snapshot_dual import KisEnvironment, load_config, split_account_no
from src.api.kis_intraday import normalize_intraday_rows
from src.ui.main_window import MainWindow
from src.ui.main_window import _extract_latest_opening_bar
from src.utils.db_loader import (
    calculate_chart_indicators,
    _get_price_history_table,
    _get_intraday_price_history_table,
    get_latest_price_history_date,
    get_latest_hourly_price_history_timestamp,
    load_symbol_history_from_db,
    load_hourly_history_from_db,
    load_intraday_history_from_db,
    prune_intraday_history,
    save_symbol_history_to_db,
    save_hourly_history_to_db,
    save_intraday_history_to_db,
    delete_intraday_history_for_symbol,
)
from src.utils.storage import load_json, save_json


def test_scanner_threshold_rules_filter_candidates():
    scanner = StockScanner()
    scanner.set_threshold_rules(
        min_volume=100_000,
        min_dollar_volume=1_000_000,
        min_adr=2.0,
        min_growth_rank=80.0,
        min_trend_intensity=70.0,
    )

    results = scanner.scan([
        {
            "symbol": "PASS",
            "volume": 200_000,
            "dollar_volume": 2_000_000,
            "adr": 3.0,
            "growth_rank": 90.0,
            "trend_intensity": 85.0,
            "price_history_days": 30,
        },
        {
            "symbol": "FAIL",
            "volume": 50_000,
            "dollar_volume": 2_000_000,
            "adr": 3.0,
            "growth_rank": 90.0,
            "trend_intensity": 85.0,
            "price_history_days": 30,
        },
    ])

    assert [item["symbol"] for item in results] == ["PASS"]


def test_position_sizer_risk_based_calculation():
    size = PositionSizer(account_size=100_000, max_risk_per_trade=0.01).size_risk_based(
        entry_price=50,
        stop_loss_price=45,
    )

    assert size.shares == 200
    assert size.risk_amount == 1000
    assert size.dollar_amount == 10_000


def test_orb_position_values_match_risk_plan_example():
    sizing = MainWindow._calculate_orb_position_values(
        account_size=2600.0,
        risk_percent=0.005,
        entry_price=15.82,
        stop_price=14.30,
        adr_percent=9.26,
    )

    assert sizing["total_risk"] == 13.0
    assert round(sizing["risk_per_share"], 2) == 1.52
    assert sizing["shares"] == 9.0
    assert round(sizing["investment"], 2) == 142.38
    assert round(sizing["capital_percent"], 1) == 5.5
    assert round(sizing["stop_loss_percent"], 2) == 9.61
    assert round(sizing["sl_adr"]) == 104


def test_orb_risk_cases_include_custom_risk_and_headers():
    cases = MainWindow._orb_risk_cases(0.03)
    headers = MainWindow._orb_position_plan_headers(cases)

    assert cases == [0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.0175, 0.02, 0.03]
    assert headers[:4] == ["Metric", "0.25% 1m", "0.25% 5m", "0.25% 30m"]
    assert "0.75% 1m" in headers
    assert "1.25% 5m" in headers
    assert "1.75% 30m" in headers
    assert "3.00% 1m" in headers


def test_orb_position_validity_requires_at_least_one_share():
    sizing = MainWindow._calculate_orb_position_values(
        account_size=1000.0,
        risk_percent=0.0,
        entry_price=100.0,
        stop_price=50.0,
        adr_percent=80.0,
    )

    assert sizing["shares"] == 0.0
    assert MainWindow._orb_position_plan_is_valid(sizing, adr_percent=80.0) is False


def test_orb_position_rounds_fractional_shares_up_before_filtering():
    sizing = MainWindow._calculate_orb_position_values(
        account_size=1000.0,
        risk_percent=0.004,
        entry_price=100.0,
        stop_price=95.0,
        adr_percent=20.0,
    )

    assert sizing["shares"] == 1.0
    assert sizing["investment"] == 100.0
    assert sizing["capital_percent"] == 10.0
    assert MainWindow._orb_position_plan_is_valid(sizing, adr_percent=20.0) is True


def test_watchlist_orb_no_entry_overrides_buy_ready_status_without_row_color():
    display_status = MainWindow._watchlist_display_status("BUY_READY", "NO_ENTRY")
    row_color = MainWindow._watchlist_status_row_color(display_status, "NO_ENTRY")

    assert display_status == "NO_ENTRY"
    assert row_color is None


def test_watchlist_buy_ready_color_uses_effective_display_status():
    display_status = MainWindow._watchlist_display_status("BUY_READY", None)
    row_color = MainWindow._watchlist_status_row_color(display_status, None)

    assert display_status == "BUY_READY"
    assert row_color.getRgb()[:3] == (39, 174, 96)


def test_watchlist_waiting_entry_status_is_green():
    display_status = MainWindow._watchlist_display_status("BUY_READY", "WAITING_ENTRY")
    row_color = MainWindow._watchlist_status_row_color(display_status, "WAITING_ENTRY")

    assert display_status == "WAITING_ENTRY"
    assert row_color.getRgb()[:3] == (39, 174, 96)


def test_watchlist_below_breakout_has_no_special_row_color():
    display_status = MainWindow._watchlist_display_status("BUY_READY", "BELOW_BREAKOUT")
    row_color = MainWindow._watchlist_status_row_color(display_status, "BELOW_BREAKOUT")

    assert display_status == "BELOW_BREAKOUT"
    assert row_color is None


def test_watchlist_score_cache_merge_preserves_orb_status():
    merged = MainWindow._merge_watchlist_score_cache(
        {"orb_status": "NO_ENTRY", "price": 100.0},
        {"status": "BUY_READY", "price": 101.0},
    )

    assert merged["orb_status"] == "NO_ENTRY"
    assert merged["status"] == "BUY_READY"
    assert merged["price"] == 101.0


def test_watchlist_orb_status_derivation_uses_only_valid_real_sizing_records():
    records = [
        {"valid": False, "sizing": {}, "entry_signal_key": "confirmed_orb_breakout", "status_reason": "invalid_sizing"},
        {"valid": True, "sizing": {"shares": 10}, "entry_signal_key": "no_entry"},
    ]

    assert MainWindow._derive_watchlist_orb_status(records) == "WAITING_ENTRY"
    assert MainWindow._derive_watchlist_orb_status([]) == "NO_INTRADAY"


def test_watchlist_orb_status_derivation_splits_no_entry_reasons():
    no_intraday = [
        {"valid": False, "sizing": {}, "status_reason": "no_intraday"},
    ]
    no_valid_orb = [
        {"valid": False, "sizing": {}, "status_reason": "no_orb"},
        {"valid": False, "sizing": {}, "status_reason": "invalid_sizing"},
    ]
    below_breakout = [
        {"valid": False, "sizing": {"shares": 10}, "status_reason": "below_breakout"},
    ]

    assert MainWindow._derive_watchlist_orb_status(no_intraday) == "NO_INTRADAY"
    assert MainWindow._derive_watchlist_orb_status(no_valid_orb) == "NO_VALID_ORB"
    assert MainWindow._derive_watchlist_orb_status(below_breakout) == "BELOW_BREAKOUT"


def test_watchlist_orb_status_derivation_detects_confirmed_entry():
    records = [
        {"valid": True, "sizing": {"shares": 10}, "entry_signal_key": "no_entry"},
        {"valid": True, "sizing": {"shares": 5}, "entry_signal_key": "confirmed_orb_breakout"},
    ]

    assert MainWindow._derive_watchlist_orb_status(records) == "BUY_READY"


def test_orb_position_validity_requires_capital_percent_between_10_and_30():
    too_small = {
        "shares": 2.0,
        "capital_percent": 9.9,
        "stop_loss_percent": 5.0,
        "sl_adr": 50.0,
    }
    valid = {
        "shares": 2.0,
        "capital_percent": 10.0,
        "stop_loss_percent": 5.0,
        "sl_adr": 50.0,
    }
    too_large = {
        "shares": 2.0,
        "capital_percent": 30.0,
        "stop_loss_percent": 5.0,
        "sl_adr": 50.0,
    }

    assert MainWindow._orb_position_plan_is_valid(too_small, adr_percent=10.0) is False
    assert MainWindow._orb_position_plan_is_valid(valid, adr_percent=10.0) is True
    assert MainWindow._orb_position_plan_is_valid(too_large, adr_percent=10.0) is False


def test_orb_position_validity_requires_sl_adr_between_15_and_66():
    too_low = {
        "shares": 2.0,
        "capital_percent": 20.0,
        "stop_loss_percent": 1.4,
        "sl_adr": 14.0,
    }
    valid = {
        "shares": 2.0,
        "capital_percent": 20.0,
        "stop_loss_percent": 1.5,
        "sl_adr": 15.0,
    }
    too_high = {
        "shares": 2.0,
        "capital_percent": 20.0,
        "stop_loss_percent": 6.7,
        "sl_adr": 67.0,
    }

    assert MainWindow._orb_position_plan_is_valid(too_low, adr_percent=10.0) is False
    assert MainWindow._orb_position_plan_is_valid(valid, adr_percent=10.0) is True
    assert MainWindow._orb_position_plan_is_valid(too_high, adr_percent=10.0) is False


def test_orb_recommendation_prefers_ideal_sl_adr_capital_and_lower_risk():
    ideal = {
        "capital_percent": 17.5,
        "sl_adr": 65.0,
    }
    less_ideal = {
        "capital_percent": 10.0,
        "sl_adr": 20.0,
    }

    ideal_score = MainWindow._score_orb_position_recommendation(ideal, risk_percent=0.0025)
    less_ideal_score = MainWindow._score_orb_position_recommendation(less_ideal, risk_percent=0.02)

    assert ideal_score > less_ideal_score
    assert MainWindow._format_orb_recommendation(ideal_score, valid=True).startswith("Excellent")
    assert MainWindow._format_orb_recommendation(ideal_score, valid=False) == "Invalid"


def test_orb_best_recommendation_prefers_higher_score_then_lower_risk():
    plans = {
        1: {"recommendation_score": 80.0, "risk_percent": 0.01},
        2: {"recommendation_score": 90.0, "risk_percent": 0.02},
        3: {"recommendation_score": 90.0, "risk_percent": 0.005},
    }

    best_column, _plan = max(
        plans.items(),
        key=lambda item: (item[1].get("recommendation_score", 0.0), -item[1].get("risk_percent", 0.0)),
    )

    assert best_column == 3


# ---------------------------------------------------------------------------
# evaluate_orb_entry_signal — all four signal states + edge cases
# ---------------------------------------------------------------------------

def test_orb_entry_signal_confirmed_when_price_above_entry_trigger():
    """Price clears both ORB high and buffered breakout price -> confirmed breakout."""
    result = evaluate_orb_entry_signal(
        orb_high=100.0,
        orb_low=95.0,
        breakout_price=102.0,
        current_price=103.0,   # above max(100, 102*1.001=102.102)
        buffer_pct=0.001,
    )

    assert isinstance(result, OrbEntrySignal)
    assert result.signal == "confirmed_orb_breakout"
    assert result.allow_entry is True
    assert result.allow_full_size is True
    assert result.suggested_size_multiplier == 1.0
    assert round(result.breakout_trigger, 4) == round(102.0 * 1.001, 4)
    assert result.entry_trigger == max(100.0, 102.0 * 1.001)


def test_orb_entry_signal_orb_only_inside_base_when_above_orb_below_breakout():
    """Price is above ORB high but below the buffered breakout -> no entry allowed."""
    result = evaluate_orb_entry_signal(
        orb_high=100.0,
        orb_low=95.0,
        breakout_price=105.0,
        current_price=101.0,   # above orb_high=100 but below breakout_trigger=105.105
        buffer_pct=0.001,
    )

    assert result.signal == "orb_only_inside_base"
    assert result.allow_entry is False
    assert result.allow_full_size is False
    assert result.suggested_size_multiplier == 0.0


def test_orb_entry_signal_probe_when_above_breakout_but_below_confirmation():
    """Probe mode: price above breakout_trigger but below entry_trigger and confirmation_price.

    For the probe branch to be reachable, entry_trigger must be > breakout_trigger.
    This happens when orb_high > breakout_trigger.  We set orb_high=110 and
    breakout_price=102 so that:
      breakout_trigger = 102 * 1.001 = 102.102
      entry_trigger    = max(110, 102.102) = 110
    A price of 105 is above breakout_trigger but below both entry_trigger and
    confirmation_price, so the probe signal fires.
    """
    result = evaluate_orb_entry_signal(
        orb_high=110.0,
        orb_low=95.0,
        breakout_price=102.0,
        current_price=105.0,   # > breakout_trigger (102.102), < entry_trigger (110), < confirmation (115)
        buffer_pct=0.001,
        confirmation_price=115.0,
        allow_probe=True,
    )

    assert round(result.breakout_trigger, 4) == round(102.0 * 1.001, 4)
    assert result.entry_trigger == 110.0
    assert result.signal == "structural_breakout_not_fully_confirmed"
    assert result.allow_entry is True
    assert result.allow_full_size is False
    assert result.suggested_size_multiplier == 0.5


def test_orb_entry_signal_no_entry_when_price_below_orb_high():
    """Price has not cleared the ORB high -> no entry."""
    result = evaluate_orb_entry_signal(
        orb_high=100.0,
        orb_low=95.0,
        breakout_price=98.0,
        current_price=97.0,    # below both orb_high and breakout_price
        buffer_pct=0.001,
    )

    assert result.signal == "no_entry"
    assert result.allow_entry is False
    assert result.allow_full_size is False


def test_orb_entry_signal_no_breakout_price_falls_back_to_orb_only():
    """When breakout_price is None, entry_trigger equals orb_high and confirmed when price exceeds it."""
    result = evaluate_orb_entry_signal(
        orb_high=100.0,
        orb_low=95.0,
        breakout_price=None,
        current_price=101.0,
        buffer_pct=0.001,
    )

    assert result.breakout_trigger == 0.0
    assert result.entry_trigger == 100.0    # falls back to orb_high
    assert result.signal == "confirmed_orb_breakout"
    assert result.allow_entry is True


def test_orb_entry_signal_entry_trigger_is_breakout_when_orb_high_is_lower():
    """When orb_high < breakout_price the entry trigger is the buffered breakout level.

    This is the key validity rule: if the ORB candle high is below the daily structural
    level, entering on the ORB high alone would be before the daily breakout is confirmed.
    The entry_trigger correctly becomes the breakout level (not the ORB high).
    """
    result = evaluate_orb_entry_signal(
        orb_high=98.0,
        orb_low=94.0,
        breakout_price=105.0,
        current_price=97.0,    # below both
        buffer_pct=0.001,
    )

    expected_trigger = 105.0 * 1.001
    assert round(result.entry_trigger, 6) == round(expected_trigger, 6)
    assert result.entry_trigger > result.orb_high  # ORB high is NOT the binding level
    assert result.signal == "no_entry"              # price hasn't cleared anything yet


def test_orb_entry_signal_buffer_applied_correctly():
    """Verify buffer_pct is applied to breakout_price before the max comparison."""
    result = evaluate_orb_entry_signal(
        orb_high=100.0,
        orb_low=95.0,
        breakout_price=100.0,
        current_price=100.05,  # above orb_high but inside the 0.1% buffer window
        buffer_pct=0.001,      # breakout_trigger = 100.1
    )

    # 100.05 > orb_high (100) but <= breakout_trigger (100.1) -> orb_only_inside_base
    assert result.breakout_trigger == 100.1
    assert result.entry_trigger == 100.1
    assert result.signal == "orb_only_inside_base"


def test_orb_plan_records_sort_best_recommendation_first():
    records = [
        {"valid": True, "recommendation_score": 70.0, "risk_percent": 0.005},
        {"valid": False, "recommendation_score": 99.0, "risk_percent": 0.0025},
        {"valid": True, "recommendation_score": 90.0, "risk_percent": 0.02},
        {"valid": True, "recommendation_score": 90.0, "risk_percent": 0.005},
    ]

    sorted_records = MainWindow._sort_orb_plan_records(records)

    assert sorted_records[0]["recommendation_score"] == 90.0
    assert sorted_records[0]["risk_percent"] == 0.005
    assert sorted_records[-1]["valid"] is False


def test_extract_latest_opening_bar_returns_first_bar_of_latest_session():
    history = pd.DataFrame(
        {
            "Open": [10.0, 10.5, 11.0, 11.5],
            "High": [10.2, 10.7, 11.2, 11.7],
            "Low": [9.8, 10.3, 10.8, 11.3],
            "Close": [10.1, 10.6, 11.1, 11.6],
            "Volume": [100.0, 200.0, 300.0, 400.0],
        },
        index=[
            pd.Timestamp("2026-01-05 14:30:00"),
            pd.Timestamp("2026-01-05 14:31:00"),
            pd.Timestamp("2026-01-06 14:30:00"),
            pd.Timestamp("2026-01-06 14:31:00"),
        ],
    )

    opening_bar = _extract_latest_opening_bar(history, "AAPL")

    assert len(opening_bar) == 1
    assert opening_bar.index[0] == pd.Timestamp("2026-01-06 14:30:00")
    assert opening_bar.iloc[0]["High"] == 11.2


def test_trade_reviewer_uses_rule_based_exit_without_rr_rejection():
    review = TradeReviewer(rulebook_dir="missing-rulebooks").review_trade(
        TradeSetup(
            symbol="ABC",
            entry_price=100,
            stop_loss=95,
            take_profit=103,
            size_shares=10,
            risk_amount=50,
            reasoning="Breakout attempt",
        ),
        use_ai=False,
    )

    assert review.approved is True
    assert not any("Risk/reward" in item or "R/R" in item for item in review.violations)
    assert any("rule-based exits" in item for item in review.recommendations)


def test_watchlist_and_trade_plan_round_trip():
    watchlist = Watchlist()
    watchlist.add("aapl", "Apple", 100.0)
    watchlist.add("AAPL", "Apple Inc.", 101.0)

    restored_watchlist = Watchlist.from_dict(watchlist.to_dict())

    assert len(restored_watchlist.items) == 1
    assert restored_watchlist.items[0].symbol == "AAPL"
    assert restored_watchlist.items[0].entry_price == 101.0

    manager = TradePlanManager()
    manager.add_plan(TradePlan(
        symbol="MSFT",
        entry_price=200,
        stop_loss=190,
        take_profit=230,
        position_size=20,
        reason="Trend continuation",
    ))

    restored_manager = TradePlanManager.from_dict(manager.to_dict())

    assert len(restored_manager.get_active_plans()) == 1
    assert restored_manager.get_active_plans()[0].symbol == "MSFT"


def test_storage_handles_missing_and_malformed_json(tmp_path: Path):
    missing_path = tmp_path / "missing.json"
    assert load_json(missing_path, {"items": []}) == {"items": []}

    path = tmp_path / "data.json"
    save_json(path, {"items": [{"symbol": "AAPL"}]})
    assert load_json(path, {})["items"][0]["symbol"] == "AAPL"

    path.write_text("{bad json", encoding="utf-8")
    assert load_json(path, {"fallback": True}) == {"fallback": True}


def test_tab_options_normalization_defaults_legacy_chart_tabs_hidden():
    options = MainWindow._normalize_tab_options({})

    assert options["tradingview"] is True
    assert options["charts"] is False
    assert options["intraday_charts"] is False


def test_tab_options_normalization_accepts_file_shape():
    options = MainWindow._normalize_tab_options({"tabs": {"charts": True, "intraday_charts": True}})

    assert options["charts"] is True
    assert options["intraday_charts"] is True
    assert options["tradingview"] is True


def test_scanner_setup_normalization_handles_custom_names():
    setups = MainWindow._normalize_scanner_setups({
        "setups": {
            "Breakout Setup": {
                "min_volume": "100000",
                "min_dollar_volume": "2500000",
                "min_adr": "3.5",
                "min_growth_rank": "96",
                "min_trend_intensity": "88",
            },
            "bad": "not-a-dict",
        }
    })

    assert list(setups.keys()) == ["Breakout Setup"]
    assert setups["Breakout Setup"]["min_volume"] == 100000.0
    assert setups["Breakout Setup"]["min_adr"] == 3.5


def test_chart_symbol_filter_matches_prefix_only():
    symbols = ["AAPL", "AA", "MSFT", "AMZN", "BA", "a"]

    assert MainWindow._filter_symbols_by_prefix(symbols, "") == ["A", "AA", "AAPL", "AMZN", "BA", "MSFT"]
    assert MainWindow._filter_symbols_by_prefix(symbols, "A") == ["A", "AA", "AAPL", "AMZN"]
    assert MainWindow._filter_symbols_by_prefix(symbols, "AA") == ["AA", "AAPL"]


def test_chart_visible_time_window_uses_selected_bar_range():
    dates = pd.date_range("2026-01-01", periods=25, freq="D")
    history = pd.DataFrame(
        {
            "Open": [float(index) for index in range(25)],
            "High": [float(index) + 1.0 for index in range(25)],
            "Low": [float(index) - 1.0 for index in range(25)],
            "Close": [float(index) + 0.5 for index in range(25)],
            "Volume": [100.0 + index for index in range(25)],
        },
        index=dates,
    )

    start, end = MainWindow._get_visible_time_window(history, {"visible_bars": 20, "visible_end": 22})

    assert start == pd.Timestamp("2026-01-03")
    assert end == pd.Timestamp("2026-01-22")


def test_local_chart_html_does_not_embed_tradingview():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0, 11.5, 12.5],
            "High": [11.0, 12.0, 13.0, 12.2, 13.2],
            "Low": [9.0, 10.0, 11.0, 10.8, 11.8],
            "Close": [10.5, 11.5, 11.4, 12.0, 12.9],
            "Volume": [1000, 1500, 1200, 1800, 1600],
        },
        index=pd.date_range("2026-01-01", periods=5, freq="D"),
    )

    chart_html = MainWindow._generate_local_chart_html("AAPL", history)

    assert "<svg" in chart_html
    assert "<rect" in chart_html
    assert "chart-hit-area" in chart_html
    assert "T target | D draw | E erase" not in chart_html
    assert "Breakout Price:" in chart_html
    assert "setChartTarget" in chart_html
    assert "clearChartTarget" in chart_html
    assert "EMA 10" in chart_html
    assert "EMA 20" in chart_html
    assert "EMA 50" in chart_html
    assert "ADR" in chart_html
    assert "tradingview.com" not in chart_html.lower()
    assert "<iframe" not in chart_html.lower()


def test_tradingview_widget_html_uses_watchlist_symbol():
    chart_html = MainWindow._generate_tradingview_widget_html("AAPL")
    chart_url = MainWindow._generate_tradingview_chart_url("NYSE:PLTR")

    assert "embed-widget-advanced-chart.js" in chart_html
    assert '"symbol": "AAPL"' in chart_html
    assert '"allow_symbol_change": true' in chart_html
    assert chart_url == "https://www.tradingview.com/chart/?symbol=NYSE%3APLTR"


def test_tradingview_symbol_mapping_supports_korean_suffixes():
    assert MainWindow._to_tradingview_symbol("AAPL") == "AAPL"
    assert MainWindow._to_tradingview_symbol("005930.KS") == "KRX:005930"
    assert MainWindow._to_tradingview_symbol("091990.KQ") == "KOSDAQ:091990"
    assert MainWindow._to_tradingview_symbol("NYSE:PLTR") == "NYSE:PLTR"


def test_tradingview_lightweight_chart_html_uses_local_ohlcv_data():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [11.0, 12.0],
            "Low": [9.0, 10.0],
            "Close": [10.5, 11.5],
            "Volume": [1000, 1200],
        },
        index=pd.date_range("2026-01-01", periods=2, freq="D"),
    )

    chart_html = MainWindow._generate_tradingview_lightweight_chart_html(
        "AAPL",
        history,
        options={"timeframe": "1D", "show_volume": True, "show_ema": True},
        drawings=[
            {
                "id": "line-1",
                "type": "line",
                "start_date": "2026-01-01",
                "start_price": 10.5,
                "end_date": "2026-01-02",
                "end_price": 11.5,
            }
        ],
        storage_symbol="AAPL",
        target_price=12.0,
    )

    assert "lightweight-charts@4.2.3" in chart_html
    assert "createChart" in chart_html
    assert '"time": "2026-01-01"' in chart_html
    assert '"close": 11.5' in chart_html
    assert "ADR" in chart_html
    assert "1M" in chart_html
    assert "EMA 10" in chart_html
    assert "addLineSeries" in chart_html
    assert "lastValueVisible: true" not in chart_html
    assert "baseLineVisible: false" in chart_html
    assert "createPriceLine" in chart_html
    assert "Breakout $" in chart_html
    assert "setChartTarget" in chart_html
    assert "clearChartTarget" in chart_html
    assert "clearTargetPrice" in chart_html
    assert "futureWhitespace" in chart_html
    assert "setVisibleLogicalRange" in chart_html
    assert "fixRightEdge: false" in chart_html
    assert "rightOffset: 40" in chart_html
    assert "subscribeVisibleLogicalRangeChange" in chart_html
    assert "const futureBars = Math.min(40" in chart_html
    assert "resetFullView" in chart_html
    assert "line-1" in chart_html
    assert "saveChartDrawing" in chart_html
    assert "deleteChartDrawing" in chart_html
    assert "pointer-events: none" in chart_html
    assert "enableLineToolMode" in chart_html
    assert "embed-widget-advanced-chart.js" not in chart_html


def test_tradingview_lightweight_chart_html_includes_rs_ti65_indicator():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 10_000_000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    indicators = pd.DataFrame(
        {
            "relative_strength": [1.0, 1.1, 1.2],
            "rs_sma_50": [1.0, 1.05, 1.1],
            "rs_score_current": [50.0, 75.0, 90.0],
            "rs_score_yesterday": [None, 50.0, 75.0],
            "rs_score_week": [None, None, None],
            "rs_score_month": [None, None, None],
            "is_ti65_bullish": [False, True, True],
            "is_ti65_bearish": [False, False, False],
            "is_9m_volume": [False, False, True],
            "is_plus_4pct_change": [False, False, True],
            "is_minus_4pct_change": [False, False, False],
            "is_rs_cross_up": [False, True, False],
        },
        index=history.index,
    )

    chart_html = MainWindow._generate_tradingview_lightweight_chart_html(
        "AAPL",
        history,
        options={"timeframe": "1D", "show_volume": True, "show_ema": False, "show_rs": True},
        indicators=indicators,
    )

    assert "RS vs SPY" in chart_html
    assert "RS SMA 50" in chart_html
    assert 'id="rs-chart"' in chart_html
    assert "RS Score C 90" in chart_html
    assert "+4%" in chart_html
    assert '"text": "Cross"' not in chart_html
    assert '"shape": "arrowUp"' not in chart_html


def test_tradingview_indicator_alignment_accepts_date_column_and_timezone():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC"),
    )
    indicators = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=3, freq="D"),
            "relative_strength": [1.0, 1.1, 1.2],
            "rs_sma_50": [1.0, 1.05, 1.1],
        }
    )

    aligned = MainWindow._align_chart_indicators(history, indicators)
    chart_html = MainWindow._generate_tradingview_lightweight_chart_html(
        "AAPL",
        history,
        options={"timeframe": "1D", "show_volume": False, "show_ema": False, "show_rs": True},
        indicators=indicators,
    )

    assert list(aligned["relative_strength"]) == [1.0, 1.1, 1.2]
    assert '"value": 1.2' in chart_html


def test_tradingview_intraday_chart_projects_daily_drawings_to_bar_times():
    history = pd.DataFrame(
        {
            "Open": [10.0, 10.5, 11.0, 11.5],
            "High": [11.0, 11.2, 12.0, 12.2],
            "Low": [9.5, 10.0, 10.8, 11.2],
            "Close": [10.5, 11.0, 11.5, 12.0],
            "Volume": [1000, 1100, 1200, 1300],
        },
        index=pd.to_datetime([
            "2026-01-01 09:30",
            "2026-01-01 09:35",
            "2026-01-02 09:30",
            "2026-01-02 09:35",
        ]),
    )

    chart_html = MainWindow._generate_tradingview_lightweight_chart_html(
        "AAPL",
        history,
        options={"timeframe": "5M", "show_volume": True, "show_ema": False},
        drawings=[
            {
                "id": "daily-line",
                "type": "line",
                "start_date": "2026-01-01",
                "start_price": 10.0,
                "end_date": "2026-01-02",
                "end_price": 12.0,
            }
        ],
    )

    start_epoch = int(pd.Timestamp("2026-01-01 09:30").tz_localize("UTC").timestamp())
    end_epoch = int(pd.Timestamp("2026-01-02 09:35").tz_localize("UTC").timestamp())
    assert '"daily-line"' in chart_html
    assert f'"time": {start_epoch}' in chart_html
    assert f'"time": {end_epoch}' in chart_html
    assert "timeVisible: true" in chart_html


def test_tradingview_intraday_chart_keeps_future_date_drawings_in_future():
    history = pd.DataFrame(
        {
            "Open": [10.0, 10.5],
            "High": [11.0, 11.2],
            "Low": [9.0, 10.0],
            "Close": [10.5, 11.0],
            "Volume": [1000, 1200],
        },
        index=pd.to_datetime(["2026-01-02 14:30", "2026-01-02 15:30"], utc=True),
    )

    chart_html = MainWindow._generate_tradingview_lightweight_chart_html(
        "AAPL",
        history,
        options={"timeframe": "1H", "show_volume": False, "show_ema": False},
        drawings=[
            {
                "id": "future-line",
                "type": "line",
                "start_date": "2026-01-05",
                "start_price": 10.5,
                "end_date": "2026-01-06",
                "end_price": 11.0,
            }
        ],
        storage_symbol="AAPL",
    )

    start_epoch = int(pd.Timestamp("2026-01-05").tz_localize("UTC").timestamp())
    end_epoch = int(pd.Timestamp("2026-01-06").tz_localize("UTC").timestamp())
    assert '"future-line"' in chart_html
    assert f'"start": {{"time": {start_epoch}' in chart_html
    assert f'"end": {{"time": {end_epoch}' in chart_html


def test_tradingview_render_helper_targets_requested_timeframe(monkeypatch):
    class DummyView:
        def __init__(self):
            self.text = ""

        def setPlainText(self, text):
            self.text = text

    history = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [1000],
        },
        index=pd.date_range("2026-01-01", periods=1, freq="D"),
    )
    window = MainWindow.__new__(MainWindow)
    window.chart_drawings = {}
    window.tradingview_refresh_timestamps = {}
    window._load_chart_history_for_timeframe = lambda symbol, timeframe, use_live_fallback=True, window_days=7: history
    monkeypatch.setattr(MainWindow, "_generate_tradingview_lightweight_chart_html", staticmethod(lambda *args, **kwargs: "<html>ok</html>"))

    status = window._render_tradingview_chart_view(
        DummyView(),
        symbol="AAPL",
        tradingview_symbol="AAPL",
        timeframe="1H",
        base_options={"show_volume": True, "show_ema": True},
        now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        force=True,
        view_key="right",
    )

    assert status == "Loaded 1H chart for AAPL"
    assert "right|AAPL|1H|volume=1|ema=1|rs=1|adr=0|g1=0|g3=0|g6=0|window=7" in window.tradingview_refresh_timestamps


def test_tradingview_indicator_reference_history_is_normalized_from_multiindex():
    chart_history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    reference_history = pd.DataFrame(
        {
            ("SPY", "Open"): [100.0, 101.0, 102.0],
            ("SPY", "High"): [101.0, 102.0, 103.0],
            ("SPY", "Low"): [99.0, 100.0, 101.0],
            ("SPY", "Close"): [100.5, 101.5, 102.5],
            ("SPY", "Volume"): [10000, 11000, 12000],
        },
        index=chart_history.index,
    )
    reference_history.columns = pd.MultiIndex.from_tuples(reference_history.columns)
    window = MainWindow.__new__(MainWindow)
    window.db_enabled = False
    window.db_engine = None
    window._load_chart_history_for_timeframe = lambda symbol, timeframe, use_live_fallback=True: reference_history

    indicators = window._load_tradingview_indicator_history("AAPL", "1D", chart_history)

    assert not indicators.empty
    assert "relative_strength" in indicators.columns


def test_tradingview_passive_refresh_is_due_every_five_minutes():
    now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)

    assert MainWindow._tradingview_refresh_due(None, now=now) is True
    assert MainWindow._tradingview_refresh_due(now - dt.timedelta(seconds=299), now=now) is False
    assert MainWindow._tradingview_refresh_due(now - dt.timedelta(seconds=300), now=now) is True


def test_latest_price_history_date_returns_max_cached_market_date():
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(
            insert(price_history),
            [
                {
                    "symbol": "AAPL",
                    "date": dt.datetime(2026, 1, 2),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "adj_close": 10.5,
                    "volume": 1000.0,
                    "updated_at": dt.datetime(2026, 1, 3),
                },
                {
                    "symbol": "MSFT",
                    "date": dt.datetime(2026, 1, 5),
                    "open": 20.0,
                    "high": 21.0,
                    "low": 19.0,
                    "close": 20.5,
                    "adj_close": 20.5,
                    "volume": 2000.0,
                    "updated_at": dt.datetime(2026, 1, 6),
                },
            ],
        )

    assert get_latest_price_history_date(engine) == dt.datetime(2026, 1, 5)


def test_price_history_and_hourly_history_use_separate_tables():
    engine = create_engine("sqlite:///:memory:", future=True)

    daily = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [12.0],
            "Low": [9.0],
            "Close": [11.0],
            "Adj Close": [11.0],
            "Volume": [1000.0],
        },
        index=[pd.Timestamp("2026-01-05")],
    )
    hourly = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [11.0, 12.0],
            "Low": [9.5, 10.5],
            "Close": [10.5, 11.5],
            "Volume": [500.0, 700.0],
        },
        index=[pd.Timestamp("2026-01-05 14:30:00"), pd.Timestamp("2026-01-05 15:30:00")],
    )

    assert save_symbol_history_to_db("AAPL", daily, engine, interval="1d") is True
    assert save_hourly_history_to_db("AAPL", hourly, engine, source="test") is True

    loaded_daily = load_symbol_history_from_db("AAPL", engine, interval="1d")
    loaded_hourly = load_hourly_history_from_db("AAPL", engine, source="test")

    assert len(loaded_daily) == 1
    assert len(loaded_hourly) == 2
    assert loaded_daily.iloc[-1]["Close"] == 11.0
    assert loaded_hourly.iloc[-1]["Close"] == 11.5
    assert get_latest_price_history_date(engine, interval="1d") == dt.datetime(2026, 1, 5)
    assert get_latest_hourly_price_history_timestamp(engine, symbol="AAPL", source="test") == dt.datetime(2026, 1, 5, 15, 30)


def test_intraday_history_round_trip_and_prune():
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    intraday_table = _get_intraday_price_history_table(metadata)
    metadata.create_all(engine)

    recent_time = pd.Timestamp.now(tz="UTC").tz_convert(None) - pd.Timedelta(days=1)
    old_time = pd.Timestamp.now(tz="UTC").tz_convert(None) - pd.Timedelta(days=10)
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [11.0, 12.0],
            "Low": [9.0, 10.0],
            "Close": [10.5, 11.5],
            "Volume": [1000.0, 2000.0],
        },
        index=[old_time, recent_time],
    )

    assert save_intraday_history_to_db("AAPL", history, engine, interval="5m", source="test") is True
    loaded = load_intraday_history_from_db("AAPL", engine, interval="5m", source="test")

    assert len(loaded) == 2
    assert loaded.iloc[-1]["Close"] == 11.5
    assert prune_intraday_history(engine, keep_days=7) == 1
    loaded_after_prune = load_intraday_history_from_db("AAPL", engine, interval="5m", source="test")
    assert len(loaded_after_prune) == 1
    assert delete_intraday_history_for_symbol(engine, "AAPL") == 1
    assert load_intraday_history_from_db("AAPL", engine, interval="5m", source="test").empty


def test_chart_indicators_calculate_rs_ti65_and_markers():
    dates = pd.date_range("2026-01-01", periods=70, freq="D")
    closes = [100.0 + index for index in range(70)]
    closes[-1] = closes[-2] * 1.05
    history = pd.DataFrame(
        {
            "Close": closes,
            "Volume": [1_000_000.0] * 69 + [10_000_000.0],
        },
        index=dates,
    )
    spy_history = pd.DataFrame(
        {
            "Close": [100.0] * 70,
            "Volume": [50_000_000.0] * 70,
        },
        index=dates,
    )

    indicators = calculate_chart_indicators("AAPL", history, spy_history)

    latest = indicators.iloc[-1]
    assert latest["symbol"] == "AAPL"
    assert latest["relative_strength"] > latest["rs_sma_50"]
    assert bool(latest["is_plus_4pct_change"]) is True
    assert bool(latest["is_9m_volume"]) is True
    assert latest["ti65"] > 1.0


def test_chart_html_includes_indicator_panel_when_indicators_available():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 10_000_000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    indicators = pd.DataFrame(
        {
            "relative_strength": [1.0, 1.1, 1.2],
            "rs_sma_50": [1.0, 1.05, 1.1],
            "rs_score_current": [50.0, 75.0, 90.0],
            "rs_score_yesterday": [None, 50.0, 75.0],
            "rs_score_week": [None, None, None],
            "rs_score_month": [None, None, None],
            "is_ti65_bullish": [False, True, True],
            "is_ti65_bearish": [False, False, False],
            "is_9m_volume": [False, False, True],
            "is_plus_4pct_change": [False, False, True],
            "is_minus_4pct_change": [False, False, False],
            "is_rs_cross_up": [False, True, False],
        },
        index=history.index,
    )

    chart_html = MainWindow._generate_local_chart_html("AAPL", history, indicators=indicators)

    assert "Relative Strength vs SPY" in chart_html
    assert "RS above SMA" in chart_html
    assert 'y2="790"' in chart_html
    assert "RS SMA" in chart_html
    assert '"relative_strength": 1.0' in chart_html


def test_chart_html_respects_visibility_options():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "High": [11.0, 12.0, 13.0, 14.0, 15.0],
            "Low": [9.0, 10.0, 11.0, 12.0, 13.0],
            "Close": [10.5, 11.5, 12.5, 13.5, 14.5],
            "Volume": [1000, 1500, 2000, 2500, 3000],
        },
        index=pd.date_range("2026-01-01", periods=5, freq="D"),
    )
    indicators = pd.DataFrame(
        {
            "relative_strength": [1.0, 1.1, 1.2, 1.3, 1.4],
            "rs_sma_50": [1.0, 1.05, 1.1, 1.2, 1.3],
        },
        index=history.index,
    )

    chart_html = MainWindow._generate_local_chart_html(
        "AAPL",
        history,
        indicators=indicators,
        options={
            "show_volume": False,
            "show_rs": False,
            "show_ema": False,
            "show_adr": False,
            "show_growth_1m": False,
            "show_growth_3m": False,
            "show_growth_6m": False,
        },
    )

    assert ">Volume<" not in chart_html
    assert "Relative Strength vs SPY" not in chart_html
    assert "EMA 10" not in chart_html
    assert "ADR" not in chart_html
    assert 'y1="560"' in chart_html


def test_chart_html_renders_saved_breakout_price():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )

    chart_html = MainWindow._generate_local_chart_html("AAPL", history, target_price=12.0)

    assert 'id="target-layer" style="display:block;"' in chart_html
    assert 'id="target-left-label"' not in chart_html
    assert 'id="target-label"' in chart_html
    assert ">Breakout Price: 12.00<" in chart_html
    assert 'id="target-label-bg" x="1000.0"' in chart_html
    assert "target-drag-hit" in chart_html
    assert "target-delete-bg" in chart_html


def test_intraday_chart_can_render_shared_breakout_price():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01 09:30", periods=3, freq="min"),
    )

    chart_html = MainWindow._generate_local_chart_html(
        "AAPL",
        history,
        target_price=12.0,
        options={"show_volume": True, "show_rs": False, "show_ema": False},
    )

    assert ">Breakout Price: 12.00<" in chart_html
    assert "setChartTarget" in chart_html


def test_intraday_chart_can_render_shared_drawings():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01 09:30", periods=3, freq="min"),
    )
    drawings = [
        {
            "id": "intraday-line",
            "type": "line",
            "start_date": "2026-01-01",
            "start_price": 10.0,
            "end_date": "2026-01-01",
            "end_price": 12.5,
        }
    ]

    chart_html = MainWindow._generate_local_chart_html(
        "AAPL",
        history,
        drawings=drawings,
        options={"show_volume": True, "show_rs": False, "show_ema": False},
    )

    assert 'data-drawing-id="intraday-line"' in chart_html
    assert "saveChartDrawing" in chart_html


def test_intraday_chart_keeps_full_window_for_daily_drawings():
    index = pd.date_range("2026-01-01 09:30", periods=300, freq="5min")
    history = pd.DataFrame(
        {
            "Open": [10.0] * len(index),
            "High": [11.0] * len(index),
            "Low": [9.0] * len(index),
            "Close": [10.5] * len(index),
            "Volume": [1000] * len(index),
        },
        index=index,
    )
    drawings = [
        {
            "id": "older-intraday-line",
            "type": "line",
            "start_date": "2026-01-01",
            "start_price": 10.0,
            "end_date": "2026-01-02",
            "end_price": 12.0,
        }
    ]

    chart_html = MainWindow._generate_local_chart_html(
        "AAPL",
        history,
        drawings=drawings,
        options={
            "show_volume": True,
            "show_rs": False,
            "show_ema": False,
            "max_history_bars": 2000,
            "visible_bars": 2000,
            "intraday_chart": True,
        },
    )

    assert 'data-drawing-id="older-intraday-line"' in chart_html


def test_intraday_chart_maps_daily_drawing_dates_to_session_span():
    index = pd.date_range("2026-01-01 09:30", periods=16, freq="h")
    history = pd.DataFrame(
        {
            "Open": [10.0] * len(index),
            "High": [11.0] * len(index),
            "Low": [9.0] * len(index),
            "Close": [10.5] * len(index),
            "Volume": [1000] * len(index),
        },
        index=index,
    )
    drawings = [
        {
            "id": "daily-to-intraday-line",
            "type": "line",
            "start_date": "2026-01-01",
            "start_price": 10.0,
            "end_date": "2026-01-01",
            "end_price": 12.0,
        }
    ]

    chart_html = MainWindow._generate_local_chart_html(
        "AAPL",
        history,
        drawings=drawings,
        options={
            "show_volume": True,
            "show_rs": False,
            "show_ema": False,
            "max_history_bars": 2000,
            "visible_bars": 2000,
            "intraday_chart": True,
        },
    )

    assert 'data-drawing-id="daily-to-intraday-line"' in chart_html
    assert "drawing-start-endpoint" in chart_html
    assert "drawing-end-endpoint" in chart_html


def test_intraday_chart_clamps_daily_drawing_dates_to_available_cache():
    index = pd.date_range("2026-01-02 09:30", periods=8, freq="h")
    history = pd.DataFrame(
        {
            "Open": [10.0] * len(index),
            "High": [11.0] * len(index),
            "Low": [9.0] * len(index),
            "Close": [10.5] * len(index),
            "Volume": [1000] * len(index),
        },
        index=index,
    )
    drawings = [
        {
            "id": "clamped-daily-line",
            "type": "line",
            "start_date": "2026-01-01",
            "start_price": 9.5,
            "end_date": "2026-01-03",
            "end_price": 12.5,
        }
    ]

    chart_html = MainWindow._generate_local_chart_html(
        "AAPL",
        history,
        drawings=drawings,
        options={
            "show_volume": True,
            "show_rs": False,
            "show_ema": False,
            "max_history_bars": 2000,
            "visible_bars": 2000,
            "intraday_chart": True,
        },
    )

    assert 'data-drawing-id="clamped-daily-line"' in chart_html


def test_chart_html_renders_editable_drawing_endpoints():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    drawings = [
        {
            "id": "editable-line",
            "type": "line",
            "start_date": "2026-01-01",
            "start_price": 10.0,
            "end_date": "2026-01-03",
            "end_price": 12.5,
        }
    ]

    chart_html = MainWindow._generate_local_chart_html("AAPL", history, drawings=drawings)

    assert 'data-drawing-id="editable-line"' in chart_html
    assert "drawing-start-endpoint" in chart_html
    assert "drawing-end-endpoint" in chart_html
    assert "updateChartDrawing" in chart_html
    assert "pointer-events:stroke" in chart_html
    assert "createEndpointHandle" in chart_html
    assert "data-start-date" in chart_html


def test_daily_chart_renders_both_handles_for_intraday_drawing_dates():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    drawings = [
        {
            "id": "intraday-made-line",
            "type": "line",
            "start_date": "2026-01-01 09:30:00",
            "start_price": 10.0,
            "end_date": "2026-01-03 15:55:00",
            "end_price": 12.5,
        }
    ]

    chart_html = MainWindow._generate_local_chart_html("AAPL", history, drawings=drawings)

    assert 'data-drawing-id="intraday-made-line"' in chart_html
    assert chart_html.count("drawing-start-endpoint") == 1
    assert chart_html.count("drawing-end-endpoint") == 1


def test_chart_html_renders_saved_drawings_and_drawing_mode():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    drawings = [
        {
            "id": "line-1",
            "type": "line",
            "start_date": "2026-01-01",
            "start_price": 10.0,
            "end_date": "2026-01-03",
            "end_price": 12.5,
        }
    ]

    chart_html = MainWindow._generate_local_chart_html("AAPL", history, drawings=drawings)

    assert 'id="drawing-layer"' in chart_html
    assert "saved-drawing-line" in chart_html
    assert 'data-drawing-id="line-1"' in chart_html
    assert "drawing-hit-line" in chart_html
    assert "enableDrawingMode" in chart_html
    assert "enableEraseMode" in chart_html
    assert "clearAllDrawings" in chart_html
    assert "saveChartDrawing" in chart_html
    assert "deleteChartDrawing" in chart_html
    assert "T target | D draw | E erase" not in chart_html
    assert chart_html.index('id="chart-hit-area"') < chart_html.index('id="drawing-layer"')
    assert 'hitArea.style.pointerEvents = "none"' in chart_html


def test_drawings_can_extend_five_weekdays_into_future():
    assert MainWindow._future_weekday_dates(pd.Timestamp("2026-01-02"), days=5) == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ]


def test_chart_html_renders_future_dated_drawing_within_limit():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    drawings = [
        {
            "id": "future-line",
            "type": "line",
            "start_date": "2026-01-03",
            "start_price": 12.0,
            "end_date": "2026-01-08",
            "end_price": 14.0,
        }
    ]

    chart_html = MainWindow._generate_local_chart_html(
        "AAPL",
        history,
        drawings=drawings,
        options={"visible_bars": 20, "visible_end": 8},
    )

    assert '"2026-01-08"' in chart_html
    assert 'data-drawing-id="future-line"' in chart_html


def test_chart_drawing_delete_updates_state():
    window = MainWindow.__new__(MainWindow)
    window.chart_drawings = {
        "AAPL": [
            {
                "id": "line-1",
                "type": "line",
                "start_date": "2026-01-01",
                "start_price": 10.0,
                "end_date": "2026-01-03",
                "end_price": 12.0,
            }
        ]
    }
    window.chart_erase_line_button = type("Button", (), {"setText": lambda self, text: None, "setStyleSheet": lambda self, style: None})()
    window.append_log = lambda message: None
    window._save_state = lambda: None

    window.delete_chart_drawing("AAPL", "line-1")

    assert "AAPL" not in window.chart_drawings


def test_chart_drawing_update_replaces_saved_line():
    window = MainWindow.__new__(MainWindow)
    window.chart_drawings = {
        "AAPL": [
            {
                "id": "line-1",
                "type": "line",
                "start_date": "2026-01-01",
                "start_price": 10.0,
                "end_date": "2026-01-03",
                "end_price": 12.0,
            }
        ]
    }
    window.append_log = lambda message: None
    window._save_state = lambda: None

    window.update_chart_drawing(
        "AAPL",
        '{"id":"line-1","type":"line","start_date":"2026-01-02","start_price":11.1,"end_date":"2026-01-04","end_price":13.2}',
    )

    drawing = window.chart_drawings["AAPL"][0]
    assert drawing["start_date"] == "2026-01-02"
    assert drawing["start_price"] == 11.1
    assert drawing["end_date"] == "2026-01-04"
    assert drawing["end_price"] == 13.2



def test_chart_html_selections_check_modes():
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    chart_html = MainWindow._generate_tradingview_lightweight_chart_html("AAPL", history, drawings=[])
    
    assert "const selected = drawing.id === selectedDrawingId && (editMode || drawingMode || eraseMode || lineToolMode);" in chart_html
    assert "if (persist) {" in chart_html


def test_get_js_key_condition():
    assert MainWindow._get_js_key_condition("T") == "event.key && event.key.toLowerCase() === 't'"
    assert MainWindow._get_js_key_condition("Ctrl+T") == "event.ctrlKey && event.key && event.key.toLowerCase() === 't'"
    assert MainWindow._get_js_key_condition("Up") == "event.key && event.key.toLowerCase() === 'arrowup'"
    assert MainWindow._get_js_key_condition("Ctrl+Shift+Down") == "event.ctrlKey && event.shiftKey && event.key && event.key.toLowerCase() === 'arrowdown'"
    assert MainWindow._get_js_key_condition("") == "false"


def test_chart_header_metrics_are_selectable():
    history = pd.DataFrame(
        {
            "Open": [100.0 + index for index in range(130)],
            "High": [102.0 + index for index in range(130)],
            "Low": [98.0 + index for index in range(130)],
            "Close": [100.0 + index for index in range(130)],
            "Volume": [1000.0] * 130,
        },
        index=pd.date_range("2026-01-01", periods=130, freq="D"),
    )

    text = MainWindow._format_chart_header_metrics(
        history,
        {
            "show_adr": True,
            "show_growth_1m": True,
            "show_growth_3m": False,
            "show_growth_6m": True,
        },
    )

    assert "Close 229.00" in text
    assert "ADR" in text
    assert "1M" in text
    assert "3M" not in text
    assert "6M" in text


def test_tradingview_step_uses_sidebar_symbol_order():
    class Combo:
        def __init__(self):
            self.items = ["AAPL", "MSFT", "TSLA", "ZZZ"]
            self.current = "AAPL"

        def count(self):
            return len(self.items)

        def itemText(self, index):
            return self.items[index]

        def currentText(self):
            return self.current

        def findText(self, text):
            return self.items.index(text) if text in self.items else -1

        def setCurrentIndex(self, index):
            self.current = self.items[index]

        def isEditable(self):
            return True

        def setEditText(self, text):
            self.current = text

    class Item:
        def __init__(self, symbol):
            self.symbol = symbol

        def data(self, role):
            return {"symbol": self.symbol}

    class ListWidget:
        def __init__(self):
            self.items = [Item("MSFT"), Item("TSLA")]
            self.current_row = None

        def count(self):
            return len(self.items)

        def item(self, row):
            return self.items[row]

        def setCurrentRow(self, row):
            self.current_row = row

    window = MainWindow.__new__(MainWindow)
    window.tradingview_symbol_combo = Combo()
    window.sidebar_stock_list = ListWidget()
    window.load_tradingview_chart = lambda force=False: None

    window.step_tradingview_watchlist_symbol(1)

    assert window.tradingview_symbol_combo.currentText() == "TSLA"
    assert window.sidebar_stock_list.current_row == 1


def test_tradingview_add_current_symbol_to_watchlist():
    class Combo:
        def currentText(self):
            return "MSFT"

    window = MainWindow.__new__(MainWindow)
    window.tradingview_symbol_combo = Combo()
    window.watchlist = Watchlist()
    window._get_sidebar_selected_data = lambda: {"symbol": "MSFT", "name": "Microsoft"}
    window.populate_watchlist_table = lambda: None
    window.update_dashboard_summary = lambda: None
    window._save_state = lambda: None
    window.prefetch_intraday_cache_for_symbol = lambda symbol: None
    window.append_log = lambda message: None

    window.add_current_tradingview_symbol_to_watchlist()

    item = window.watchlist.get("MSFT")
    assert item is not None
    assert item.name == "Microsoft"


def test_tradingview_add_current_symbol_toggles_existing_watchlist_item():
    class Combo:
        def currentText(self):
            return "MSFT"

    window = MainWindow.__new__(MainWindow)
    window.tradingview_symbol_combo = Combo()
    window.watchlist = Watchlist()
    window.watchlist.add("MSFT", "Microsoft")
    window.populate_watchlist_table = lambda: None
    window.update_dashboard_summary = lambda: None
    window._save_state = lambda: None
    window.append_log = lambda message: None

    window.add_current_tradingview_symbol_to_watchlist()

    assert window.watchlist.get("MSFT") is None


def test_tradingview_add_current_symbol_does_not_remove_when_sidebar_not_watchlist():
    class Combo:
        def currentText(self):
            return "MSFT"

    class SidebarSourceCombo:
        def currentData(self):
            return {"type": "scan", "setup": "Breakout"}

    window = MainWindow.__new__(MainWindow)
    window.tradingview_symbol_combo = Combo()
    window.sidebar_source_combo = SidebarSourceCombo()
    window.watchlist = Watchlist()
    window.watchlist.add("MSFT", "Microsoft")
    window.populate_watchlist_table = lambda: None
    window.update_dashboard_summary = lambda: None
    window._save_state = lambda: None
    window.append_log = lambda message: None

    window.add_current_tradingview_symbol_to_watchlist()

    assert window.watchlist.get("MSFT") is not None


def test_chart_html_includes_bounded_pan_zoom_state():
    history = pd.DataFrame(
        {
            "Open": [100.0 + index for index in range(60)],
            "High": [101.0 + index for index in range(60)],
            "Low": [99.0 + index for index in range(60)],
            "Close": [100.0 + index for index in range(60)],
            "Volume": [1000.0] * 60,
        },
        index=pd.date_range("2026-01-01", periods=60, freq="D"),
    )

    chart_html = MainWindow._generate_local_chart_html(
        "AAPL",
        history,
        options={"visible_bars": 20, "visible_end": 30},
    )

    assert '"total": 60' in chart_html
    assert '"start": 10' in chart_html
    assert '"end": 30' in chart_html
    assert "updateChartWindow" in chart_html
    assert "chartBridge.updateChartWindow" in chart_html
    assert "stepChartSymbol" in chart_html
    assert "resetChartFullView" in chart_html
    assert "arrowleft" in chart_html.lower()
    assert "arrowdown" in chart_html.lower()
    assert "wheel" in chart_html
    assert "isPanningChart" in chart_html
    assert "pan-preview-layer" in chart_html
    assert "setPanPreview" in chart_html
    assert "wheelZoomTimer" in chart_html
    assert "range-navigator" in chart_html
    assert "navigator-left-handle" in chart_html
    assert "navigator-right-handle" in chart_html


def test_chart_viewport_can_include_right_side_blank_space():
    history = pd.DataFrame(
        {
            "Open": [100.0 + index for index in range(60)],
            "High": [101.0 + index for index in range(60)],
            "Low": [99.0 + index for index in range(60)],
            "Close": [100.0 + index for index in range(60)],
            "Volume": [1000.0] * 60,
        },
        index=pd.date_range("2026-01-01", periods=60, freq="D"),
    )

    chart_html = MainWindow._generate_local_chart_html(
        "AAPL",
        history,
        options={"visible_bars": 20, "visible_end": 70},
    )

    assert '"maxEnd": 75' in chart_html
    assert '"end": 70' in chart_html
    assert '"slot": 9' in chart_html


def test_update_chart_window_stores_symbol_view_state():
    window = MainWindow.__new__(MainWindow)
    window.chart_view_windows = {}
    window.selected_scan_symbol = None
    window.chart_symbol_input = type(
        "Combo",
        (),
        {
            "setText": lambda self, text: setattr(self, "value", text),
            "text": lambda self: getattr(self, "value", ""),
        },
    )()
    window.plot_selected_symbol = lambda show_warnings=False: None

    window.update_chart_window("aapl", 35, 80)

    assert window.chart_view_windows["AAPL"] == {"bars": 35, "end": 80}


def test_reset_chart_full_view_clears_symbol_view_state():
    window = MainWindow.__new__(MainWindow)
    window.chart_view_windows = {"AAPL": {"bars": 35, "end": 80}}
    window.selected_scan_symbol = None
    window.chart_symbol_input = type(
        "Input",
        (),
        {
            "setText": lambda self, text: setattr(self, "value", text),
            "text": lambda self: getattr(self, "value", "AAPL"),
        },
    )()
    window.plot_selected_symbol = lambda show_warnings=False: None

    window.reset_chart_full_view("AAPL")

    assert "AAPL" not in window.chart_view_windows


def test_market_data_status_uses_7am_kst_cutoff():
    before_cutoff = dt.datetime(2026, 6, 24, 6, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    after_cutoff = dt.datetime(2026, 6, 24, 7, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))

    assert MainWindow._expected_latest_market_data_date(before_cutoff) == dt.date(2026, 6, 22)
    assert MainWindow._expected_latest_market_data_date(after_cutoff) == dt.date(2026, 6, 23)

    assert MainWindow._format_market_data_status_from_date(dt.datetime(2026, 6, 22), before_cutoff).startswith("Up to date")
    assert MainWindow._format_market_data_status_from_date(dt.datetime(2026, 6, 22), after_cutoff).startswith("Needs refresh")


def test_market_data_status_rolls_weekends_back_to_friday():
    saturday_after_cutoff = dt.datetime(2026, 6, 27, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    monday_before_cutoff = dt.datetime(2026, 6, 29, 6, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))

    assert MainWindow._expected_latest_market_data_date(saturday_after_cutoff) == dt.date(2026, 6, 26)
    assert MainWindow._expected_latest_market_data_date(monday_before_cutoff) == dt.date(2026, 6, 26)


def test_live_intraday_refresh_uses_us_regular_market_hours():
    eastern = dt.timezone(dt.timedelta(hours=-4))
    market_open = dt.datetime(2026, 6, 24, 10, 0, tzinfo=eastern)
    premarket = dt.datetime(2026, 6, 24, 9, 0, tzinfo=eastern)
    after_close = dt.datetime(2026, 6, 24, 16, 0, tzinfo=eastern)
    weekend = dt.datetime(2026, 6, 27, 10, 0, tzinfo=eastern)

    assert MainWindow._is_us_regular_market_open(market_open) is True
    assert MainWindow._is_us_regular_market_open(premarket) is False
    assert MainWindow._is_us_regular_market_open(after_close) is False
    assert MainWindow._is_us_regular_market_open(weekend) is False


def test_kis_account_number_parser_accepts_common_formats():
    assert split_account_no("12345678") == ("12345678", "01")
    assert split_account_no("12345678", default_product_code="03") == ("12345678", "03")
    assert split_account_no("12345678-01") == ("12345678", "01")
    assert split_account_no("1234567801") == ("12345678", "01")
    assert split_account_no("12345678 01") == ("12345678", "01")


def test_kis_error_formatter_handles_rate_limit_and_invalid_account():
    assert "rate limit" in MainWindow._format_kis_error_message("KIS rate limit exceeded.").lower()
    assert "account number/product code" in MainWindow._format_kis_error_message("INPUT INVALID_CHECK_ACNO")


def test_kis_product_code_probe_masks_accounts(monkeypatch):
    def fake_snapshot(environment, include_domestic, include_overseas, account_no, force_token=False):
        if account_no.endswith("-03"):
            return {"domestic": {"holdings": [{"symbol": "005930"}]}}
        raise RuntimeError("invalid")

    monkeypatch.setattr(kis_snapshot, "fetch_account_snapshot", fake_snapshot)

    results = kis_snapshot.probe_account_product_codes(
        KisEnvironment.PROD,
        "12345678-01",
        ["01", "03"],
    )

    assert results[0]["account"] == "12******-01"
    assert results[0]["status"] == "error"
    assert results[1]["account"] == "12******-03"
    assert results[1]["status"] == "ok"


def test_kis_prod_config_falls_back_to_legacy_kis_config(monkeypatch):
    monkeypatch.setattr(kis_snapshot, "load_dotenv", None)
    for key in [
        "KIS_PROD_APP_KEY",
        "KIS_PROD_APP_SECRET",
        "KIS_PROD_BASE_URL",
        "KIS_PROD_ACCOUNT_NO",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KIS_PROD_ACCOUNT_NO", "12345678-01")
    monkeypatch.setattr(
        kis_snapshot,
        "load_legacy_prod_config",
        lambda: {
            "app_key": "legacy_key",
            "app_secret": "legacy_secret",
            "base_url": "https://legacy.example.com",
        },
    )

    config = load_config(KisEnvironment.PROD)

    assert config.app_key == "legacy_key"
    assert config.app_secret == "legacy_secret"
    assert config.base_url == "https://legacy.example.com"
    assert config.account_no_masked == "12******-01"


def test_kis_account_profile_discovery_reads_multiple_configured_accounts(monkeypatch):
    monkeypatch.setattr(kis_snapshot, "load_dotenv", None)
    for key in [
        "KIS_PROD_ACCOUNT_NO",
        "KIS_PROD_ACCOUNTS",
        "KIS_PROD_ACCOUNT_NO_1",
        "KIS_PROD_ACCOUNT_NO_2",
        "KIS_SIM_ACCOUNT_NO",
        "KIS_SIM_ACCOUNTS",
        "KIS_SIM_ACCOUNT_NO_1",
        "KIS_SIM_ACCOUNT_NO_2",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KIS_PROD_ACCOUNT_NO", "12345678-01")
    monkeypatch.setenv("KIS_PROD_ACCOUNTS", "87654321-01, bad-value, 1234567801")
    monkeypatch.setenv("KIS_PROD_ACCOUNT_NO_2", "11112222-03")

    profiles = kis_snapshot.discover_account_profiles()
    prod_profiles = [profile for profile in profiles if profile["environment"] == "PROD"]

    assert [profile["account_no"] for profile in prod_profiles] == [
        "12345678-01",
        "87654321-01",
        "11112222-03",
    ]
    assert prod_profiles[0]["label"] == "PROD 12******-01"


def test_kis_snapshot_helpers_format_summary_and_holdings():
    snapshot = {
        "fetched_at": "2026-06-24T00:00:00+00:00",
        "environment": "SIM",
        "account": "12******-01",
        "domestic": {
            "summary": {
                "cash_total_krw": 1_000_000,
                "total_evaluation_krw": 2_500_000,
                "evaluation_profit_loss_krw": 50_000,
            },
            "holdings": [{"market": "KR", "symbol": "005930"}],
        },
        "overseas": {
            "holdings": [{"market": "NASD", "symbol": "AAPL"}],
        },
    }

    summary = MainWindow._format_kis_snapshot_summary(snapshot)
    holdings = MainWindow._flatten_kis_holdings(snapshot)

    assert "Profile: SIM" in summary
    assert "cash 1,000,000 KRW" in summary
    assert "Overseas: 1 holdings loaded." in summary
    assert [item["symbol"] for item in holdings] == ["005930", "AAPL"]


def test_trade_plan_account_value_extracts_krw_total_before_cash():
    snapshot = {
        "domestic": {
            "summary": {
                "total_evaluation_krw": 10_000_000,
                "cash_total_krw": 3_000_000,
            }
        }
    }

    assert MainWindow._extract_kis_account_value_krw(snapshot) == 10_000_000


def test_trade_plan_account_value_extracts_tot_evlu_amt():
    snapshot = {
        "domestic": {
            "summary": {
                "tot_evlu_amt": 10_000_000,
                "cash_total_krw": 3_000_000,
            }
        }
    }
    assert MainWindow._extract_kis_account_value_krw(snapshot) == 10_000_000


def test_trade_plan_latest_price_updates_entry_and_stop():
    class Input:
        def __init__(self, value=""):
            self.value = value

        def text(self):
            return self.value

        def setText(self, value):
            self.value = value

        def blockSignals(self, blocked):
            return False

    window = MainWindow.__new__(MainWindow)
    window.latest_intraday_prices = {}
    window.symbol_input = Input("AAPL")
    window.entry_price_input = Input()
    window.stop_loss_input = Input()
    window.update_trade_plan_feedback = lambda: None

    window.update_trade_prices_from_latest("AAPL", 100.0)

    assert window.latest_intraday_prices["AAPL"] == 100.0
    assert window.entry_price_input.text() == "100.00"
    assert window.stop_loss_input.text() == "92.00"


def test_intraday_watchlist_step_wraps_and_plots():
    class Combo:
        def __init__(self):
            self.items = ["AAPL", "MSFT"]
            self.index = 0

        def count(self):
            return len(self.items)

        def currentIndex(self):
            return self.index

        def setCurrentIndex(self, index):
            self.index = index

        def currentText(self):
            return self.items[self.index]

    window = MainWindow.__new__(MainWindow)
    window.intraday_symbol_combo = Combo()
    window.plot_count = 0
    window.plot_intraday_watchlist_symbol = lambda: setattr(window, "plot_count", window.plot_count + 1)

    window.step_intraday_watchlist_symbol(1)
    assert window.intraday_symbol_combo.currentText() == "MSFT"
    assert window.plot_count == 1

    window.step_intraday_watchlist_symbol(1)
    assert window.intraday_symbol_combo.currentText() == "AAPL"
    assert window.plot_count == 2


def test_intraday_window_days_parser_and_backfill_decision():
    class Combo:
        def currentText(self):
            return "7D"

    window = MainWindow.__new__(MainWindow)
    window.intraday_window_combo = Combo()

    assert window._get_intraday_window_days() == 7
    since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=7)
    empty = pd.DataFrame()
    assert MainWindow._intraday_cache_needs_backfill(empty, since) is True

    full_cache = pd.DataFrame(
        {"Close": [1.0]},
        index=[pd.Timestamp(since) + pd.Timedelta(hours=1)],
    )
    assert MainWindow._intraday_cache_needs_backfill(full_cache, since) is False

    short_cache = pd.DataFrame(
        {"Close": [1.0]},
        index=[pd.Timestamp.now(tz="UTC").tz_convert(None) - pd.Timedelta(days=1)],
    )
    assert MainWindow._intraday_cache_needs_backfill(short_cache, since) is True


def test_intraday_fetch_attempt_cooldown_blocks_immediate_refetch():
    window = MainWindow.__new__(MainWindow)
    window.intraday_fetch_attempts = {}

    assert window._can_start_intraday_fetch("AAPL", 7) is True
    window.intraday_fetch_attempts[window._intraday_fetch_key("AAPL", 7)] = (
        dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    )
    assert window._can_start_intraday_fetch("AAPL", 7) is False


def test_orb_range_and_signal_use_opening_window():
    index = pd.date_range("2026-01-02 09:30", periods=6, freq="min")
    intraday = pd.DataFrame(
        {
            "Open": [10, 11, 12, 13, 14, 15],
            "High": [11, 12, 13, 14, 15, 18],
            "Low": [9, 10, 11, 12, 13, 14],
            "Close": [10.5, 11.5, 12.5, 13.5, 14.5, 17.5],
            "Volume": [100] * 6,
        },
        index=index,
    )

    orb_range = calculate_orb_range("AAPL", intraday, "5m")
    signal = evaluate_orb_signal("AAPL", intraday, "5m", target_price=17.0)

    assert orb_range is not None
    assert orb_range.high == 15
    assert orb_range.low == 9
    assert signal is not None
    assert signal.breakout == "up"
    assert signal.target_met is True


def test_intraday_resample_builds_larger_candles():
    index = pd.date_range("2026-01-02 09:30", periods=5, freq="5min")
    intraday = pd.DataFrame(
        {
            "Open": [10, 11, 12, 13, 14],
            "High": [11, 12, 13, 14, 15],
            "Low": [9, 10, 11, 12, 13],
            "Close": [10.5, 11.5, 12.5, 13.5, 14.5],
            "Volume": [100, 200, 300, 400, 500],
        },
        index=index,
    )

    resampled = resample_intraday_bars(intraday, "30m")

    assert len(resampled) == 1
    assert resampled.iloc[0]["Open"] == 10
    assert resampled.iloc[0]["High"] == 15
    assert resampled.iloc[0]["Low"] == 9
    assert resampled.iloc[0]["Close"] == 14.5
    assert resampled.iloc[0]["Volume"] == 1500


def test_kis_intraday_normalizer_maps_rows_to_ohlcv():
    result = normalize_intraday_rows(
        symbol="AAPL",
        exchange="NAS",
        rows=[
            {
                "time": "2026-01-02 09:30",
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10.5",
                "volume": "1000",
            }
        ],
        time_field="time",
        open_field="open",
        high_field="high",
        low_field="low",
        close_field="close",
        volume_field="volume",
    )

    assert result.symbol == "AAPL"
    assert result.exchange == "NAS"
    assert result.bars.iloc[0]["Close"] == 10.5


def test_compute_stock_metrics_calculates_all_fields():
    from src.utils.data_loader import compute_stock_metrics
    import pandas as pd
    import numpy as np

    dates = pd.date_range("2025-01-01", periods=300, freq="D")
    history = pd.DataFrame(
        {
            "Open": np.linspace(100.0, 150.0, 300),
            "High": np.linspace(102.0, 152.0, 300),
            "Low": np.linspace(98.0, 148.0, 300),
            "Close": np.linspace(101.0, 151.0, 300),
            "Volume": np.linspace(100000, 200000, 300),
        },
        index=dates,
    )
    
    spy_history = pd.DataFrame(
        {
            "Close": np.linspace(500.0, 550.0, 300),
        },
        index=dates,
    )

    multi_history = pd.concat([history], keys=["AAPL"], axis=1)

    result = compute_stock_metrics("AAPL", multi_history, spy_history=spy_history)

    assert result is not None
    assert result["symbol"] == "AAPL"
    assert result["price"] == 151.0
    assert result["volume"] == 200000.0
    assert result["avg_volume_20d"] > 0
    assert result["above_sma_20"] is True
    assert result["above_ema_50"] is True
    assert result["sma_200"] > 0
    assert result["high_252d"] > 0
    assert result["rs_score_252"] > 0
    assert "adr_20" in result
    assert "atr_14_pct" in result
