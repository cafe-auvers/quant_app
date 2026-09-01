import pytest
import pandas as pd
from datetime import datetime, timezone
from src.core.watchlist import BuylistItem, BuylistManager, TradePlan, TradePlanManager, Watchlist
from src.core.scoring import calculate_deterministic_scores, run_ai_review


def test_persisted_watchlist_timestamps_are_timezone_aware():
    watchlist = Watchlist.from_dict(
        {
            "name": "Default",
            "created_date": "2026-07-01T09:00:00",
            "items": [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "added_date": "2026-07-01T09:30:00",
                }
            ],
        }
    )

    assert watchlist.created_date.tzinfo == timezone.utc
    assert watchlist.items[0].added_date.tzinfo == timezone.utc


def test_buylist_item_serialization():
    item = BuylistItem(
        symbol="AAPL",
        name="Apple Inc.",
        entry_price=150.0,
        target_price=180.0,
        stop_loss=140.0,
        total_score=88.5,
        status="BUY_READY",
        technical_score=90.0,
        setup_score=85.0,
        risk_score=90.0,
        news_score=80.0,
        timing_score=95.0,
        rr=3.0,
        stop_adr=0.5,
        position_percent=17.5,
        ai_summary="Strong breakout setup.",
        warnings=[],
        notes="High conviction trade.",
    )
    
    serialized = item.to_dict()
    assert serialized["symbol"] == "AAPL"
    assert serialized["total_score"] == 88.5
    assert serialized["status"] == "BUY_READY"
    assert serialized["notes"] == "High conviction trade."
    assert serialized["auto_order_block_reason"] == ""
    assert serialized["kis_account_no"] == ""

    serialized["auto_order_block_reason"] = "Manual review required before retry."
    serialized["kis_account_no"] = "12345678-01"
    
    deserialized = BuylistItem.from_dict(serialized)
    assert deserialized.symbol == "AAPL"
    assert deserialized.total_score == 88.5
    assert deserialized.status == "BUY_READY"
    assert deserialized.notes == "High conviction trade."
    assert deserialized.rr == 3.0
    assert deserialized.stop_adr == 0.5
    assert deserialized.breakout_price == 180.0
    assert deserialized.auto_order_block_reason == "Manual review required before retry."
    assert deserialized.kis_account_no == "12345678-01"


def test_buylist_item_restores_filled_holding_as_bought_position():
    item = BuylistItem(
        symbol="STIM",
        name="STIM",
        entry_price=2.8899,
        target_price=0.0,
        stop_loss=2.73,
        total_score=65.6,
        status="FILLED",
        technical_score=0.0,
        setup_score=0.0,
        risk_score=0.0,
        news_score=0.0,
        timing_score=0.0,
        rr=0.0,
        stop_adr=43.0,
        position_percent=18.1,
        ai_summary="Execution queue FILLED",
        warnings=[],
        monitoring_status="FILLED",
        shares_held=791,
        avg_cost=2.88,
        breakout_method="execution_queue:1m",
    )

    assert item.status == "FILLED"
    assert item.monitoring_status == "BOUGHT"
    assert item.shares_held == 791
    assert item.avg_cost == 2.88


def test_legacy_sim_buylist_state_is_ignored():
    serialized = BuylistItem(
        symbol="AAPL",
        name="Apple Inc.",
        entry_price=150.0,
        target_price=180.0,
        stop_loss=140.0,
        total_score=88.5,
        status="BUY_READY",
        technical_score=90.0,
        setup_score=85.0,
        risk_score=90.0,
        news_score=80.0,
        timing_score=95.0,
        rr=3.0,
        stop_adr=0.5,
        position_percent=17.5,
        ai_summary="Strong breakout setup.",
        warnings=[],
    ).to_dict()
    serialized["environment"] = "SIM"

    restored = BuylistManager.from_dict({"items": [serialized]})

    assert restored.items == []


def test_buylist_manager():
    manager = BuylistManager()
    assert len(manager.items) == 0
    
    item1 = BuylistItem(
        symbol="AAPL", name="Apple Inc.", entry_price=150.0, target_price=180.0, stop_loss=140.0,
        total_score=90.0, status="BUY_READY", technical_score=90.0, setup_score=90.0,
        risk_score=90.0, news_score=90.0, timing_score=90.0, rr=3.0, stop_adr=0.5,
        position_percent=17.5, ai_summary="Test 1", warnings=[]
    )
    item2 = BuylistItem(
        symbol="MSFT", name="Microsoft Corp.", entry_price=300.0, target_price=360.0, stop_loss=280.0,
        total_score=92.0, status="BUY_READY", technical_score=90.0, setup_score=90.0,
        risk_score=90.0, news_score=90.0, timing_score=90.0, rr=3.0, stop_adr=0.5,
        position_percent=17.5, ai_summary="Test 2", warnings=[]
    )
    
    manager.add(item1)
    manager.add(item2)
    assert len(manager.items) == 2
    
    assert manager.get("AAPL").symbol == "AAPL"
    assert manager.get("MSFT").symbol == "MSFT"
    assert manager.get("GOOG") is None
    
    # Update item
    item1_updated = BuylistItem(
        symbol="AAPL", name="Apple Inc.", entry_price=155.0, target_price=180.0, stop_loss=140.0,
        total_score=95.0, status="BUY_READY", technical_score=95.0, setup_score=90.0,
        risk_score=90.0, news_score=90.0, timing_score=90.0, rr=2.5, stop_adr=0.6,
        position_percent=17.5, ai_summary="Test 1 Updated", warnings=[]
    )
    manager.add(item1_updated)
    assert len(manager.items) == 2
    assert manager.get("AAPL").total_score == 95.0
    assert manager.get("AAPL").entry_price == 155.0
    
    # Remove
    removed = manager.remove("AAPL")
    assert removed is True
    assert len(manager.items) == 1
    assert manager.get("AAPL") is None
    
    # Serialization
    serialized = manager.to_dict()
    new_manager = BuylistManager.from_dict(serialized)
    assert len(new_manager.items) == 1
    assert new_manager.get("MSFT").symbol == "MSFT"


def test_calculate_deterministic_scores():
    # Construct mock historical data with 25 days of consistent growth and high volume
    dates = pd.date_range(start="2026-06-01", periods=30, freq="D")
    # Generating close prices above 20 EMA and 50 EMA
    close_prices = [100.0 + i * 2.0 for i in range(30)] # 100 to 158
    high_prices = [p * 1.02 for p in close_prices]
    low_prices = [p * 0.98 for p in close_prices]
    open_prices = [p * 0.99 for p in close_prices]
    volume = [50000.0] * 30
    
    history = pd.DataFrame(
        {
            "Open": open_prices,
            "High": high_prices,
            "Low": low_prices,
            "Close": close_prices,
            "Adj Close": close_prices,
            "Volume": volume,
        },
        index=dates
    )
    
    # Legacy target_price input is migrated to breakout_price; no R/R target is scored.
    scores = calculate_deterministic_scores(
        symbol="XYZ",
        history=history,
        entry_price=160.0,
        target_price=200.0,
        stop_loss=150.0,
        account_size=100000.0,
        risk_percent=0.01,
    )
    
    assert scores["price"] == 158.0
    assert scores["technical_score"] > 0
    assert scores["setup_score"] > 0
    assert scores["risk_score"] > 0
    assert scores["rr"] == 0.0
    assert scores["target_price"] == 0.0
    assert scores["breakout_price"] == 200.0
    assert len(scores["warnings"]) == 0 or "Price is below 50-day EMA" not in scores["warnings"]
    
    # A low legacy target must not create an R/R rejection or fixed profit target.
    low_rr_scores = calculate_deterministic_scores(
        symbol="XYZ",
        history=history,
        entry_price=160.0,
        target_price=165.0,
        stop_loss=150.0,
        account_size=100000.0,
        risk_percent=0.01,
    )
    assert low_rr_scores["rr"] == 0.0
    assert low_rr_scores["target_price"] == 0.0
    assert low_rr_scores["breakout_price"] == 165.0
    assert not any("Risk/Reward" in w or "R/R" in w for w in low_rr_scores["warnings"])


def test_watchlist_legacy_target_price_migrates_to_breakout_price():
    watchlist = Watchlist.from_dict({
        "items": [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "target_price": 180.0,
            }
        ]
    })

    assert watchlist.items[0].breakout_price == 180.0
    assert watchlist.to_dict()["items"][0]["breakout_price"] == 180.0


def test_fallback_ai_review(monkeypatch):
    monkeypatch.setattr("src.core.scoring.get_env_value", lambda key: "")
    metrics = {
        "price": 100.0,
        "rr": 2.0,
        "warnings": [],
    }
    
    res = run_ai_review("XYZ", metrics, recent_news_json="[]")
    assert "Clean bullish setup" in res["summary"]
    assert res["news_score"] == 80.0
    
    # Test when warnings exist
    metrics_warn = {
        "price": 100.0,
        "rr": 1.0,
        "warnings": ["Stop loss is wider than the selected risk model"],
    }
    res_warn = run_ai_review("XYZ", metrics_warn, recent_news_json="[]")
    assert "active violations" in res_warn["summary"]
    assert res_warn["news_score"] == 50.0


def test_trade_plan_serialization_and_scaling():
    # 1. Test TradePlan serialization with risk_percent
    plan = TradePlan(
        symbol="SNDK",
        entry_price=46.65,
        stop_loss=42.92,
        take_profit=90.0,
        position_size=268,
        reason="ORB breakout",
        risk_percent=0.0025,
    )
    
    manager = TradePlanManager()
    manager.add_plan(plan)
    
    serialized = manager.to_dict()
    assert "plans" in serialized
    assert len(serialized["plans"]) == 1
    assert serialized["plans"][0]["symbol"] == "SNDK"
    assert serialized["plans"][0]["risk_percent"] == 0.0025
    
    new_manager = TradePlanManager.from_dict(serialized)
    assert len(new_manager.plans) == 1
    loaded_plan = new_manager.plans[0]
    assert loaded_plan.symbol == "SNDK"
    assert loaded_plan.risk_percent == 0.0025
    
    # 2. Test calculate_deterministic_scores dynamic risk percent calculations
    # Construct a minimal DataFrame
    history = pd.DataFrame(
        {
            "Open": [45.0, 46.0],
            "High": [46.0, 47.0],
            "Low": [44.0, 45.0],
            "Close": [45.5, 46.65],
            "Volume": [100000.0, 120000.0],
        },
        index=pd.date_range("2026-06-25", periods=2, freq="D")
    )
    
    # Scale test: small account size ($7,200) and small risk (0.25%)
    scores_small = calculate_deterministic_scores(
        symbol="SNDK",
        history=history,
        entry_price=46.65,
        target_price=90.0,
        stop_loss=42.92,
        account_size=7200.0,
        risk_percent=0.0025,  # 0.25% risk -> $18 willing to lose
    )
    # entry - stop = 3.73. $18 / 3.73 = 4.82 -> 5 shares ceiled
    # Actual risk = 5 * 3.73 = $18.65.
    # Actual risk percent = 18.65 / 7200 = 0.259% -> rounds to 0.26%
    assert scores_small["shares"] == 5
    assert scores_small["risk_percent"] == 0.26
    assert scores_small["position_percent"] == round((5 * 46.65 / 7200.0) * 100.0, 1)


def test_environment_combos_are_production_only():
    from PyQt5.QtWidgets import QComboBox
    from src.ui.main_window import MainWindow
    
    # Create QApplication instance if not present (PyQt requires it for widgets)
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    
    window = MainWindow.__new__(MainWindow)
    window.watchlist_env_combo = QComboBox()
    window.watchlist_env_combo.addItem("PROD")
    window.trade_kis_environment_combo = QComboBox()
    window.trade_kis_environment_combo.addItem("PROD")

    assert window.watchlist_env_combo.count() == 1
    assert window.watchlist_env_combo.currentText() == "PROD"
    assert window.trade_kis_environment_combo.count() == 1
    assert window.trade_kis_environment_combo.currentText() == "PROD"
