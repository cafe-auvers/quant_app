import pytest
import pandas as pd
from datetime import datetime
from src.core.watchlist import BuylistItem, BuylistManager, TradePlan, TradePlanManager, Watchlist
from src.core.scoring import calculate_deterministic_scores, run_ai_review


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

    serialized["auto_order_block_reason"] = "KIS SIM rejected overseas order routing."
    
    deserialized = BuylistItem.from_dict(serialized)
    assert deserialized.symbol == "AAPL"
    assert deserialized.total_score == 88.5
    assert deserialized.status == "BUY_READY"
    assert deserialized.notes == "High conviction trade."
    assert deserialized.rr == 3.0
    assert deserialized.stop_adr == 0.5
    assert deserialized.breakout_price == 180.0
    assert deserialized.auto_order_block_reason == "KIS SIM rejected overseas order routing."


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


def test_fallback_ai_review():
    metrics = {
        "price": 100.0,
        "rr": 2.0,
        "warnings": [],
    }
    
    res = run_ai_review("XYZ", metrics)
    assert "Clean bullish setup" in res["summary"]
    assert res["news_score"] == 80.0
    
    # Test when warnings exist
    metrics_warn = {
        "price": 100.0,
        "rr": 1.0,
        "warnings": ["Stop loss is wider than the selected risk model"],
    }
    res_warn = run_ai_review("XYZ", metrics_warn)
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


def test_watchlist_worker_dynamic_risk_search():
    from src.ui.main_window import WatchlistAiWorker
    from src.core.watchlist import WatchlistItem
    import pandas as pd
    
    item = WatchlistItem(
        symbol="HUM",
        name="Humana Inc.",
        entry_price=350.00,
        stop_loss=345.50,
        breakout_price=450.00,
    )
    
    # Mock database engine (None)
    mock_history = pd.DataFrame(
        {
            "Open": [345.0, 348.0],
            "High": [350.0, 352.0],
            "Low": [340.0, 341.4],
            "Close": [347.0, 350.0],
            "Volume": [100000.0, 120000.0],
        },
        index=pd.date_range("2026-06-25", periods=2, freq="D")
    )
    
    import unittest.mock as mock
    with mock.patch("src.utils.db_loader.load_symbol_history_from_db", return_value=mock_history), \
         mock.patch("src.utils.data_loader.download_price_history", return_value=mock_history):
         
         # Initialize worker with small account ($7,200) and default 1.0% risk
         worker = WatchlistAiWorker(
             watchlist_items=[item],
             db_engine=None,
             account_size=7200.0,
             risk_percent=0.01,  # 1.0% risk default
         )
         
         # Run worker's business logic directly
         results = {}
         def on_finished(res):
             results.update(res)
         worker.finished_analysis.connect(on_finished)
         worker.run()
         
         assert "HUM" in results
         hum_res = results["HUM"]
         
         # Verify that the final calculated capital allocation (position_percent) is valid (< 30%)
         assert hum_res["position_percent"] < 30.0
         assert hum_res["position_percent"] >= 10.0
         assert hum_res["risk_percent"] < 1.0  # scaled down from 1.0% to a valid lower risk case


def test_watchlist_worker_df_emission():
    from src.ui.main_window import WatchlistAiWorker
    from src.core.watchlist import WatchlistItem
    import pandas as pd
    
    item = WatchlistItem(
        symbol="HUM",
        name="Humana Inc.",
        entry_price=350.00,
        stop_loss=345.50,
        breakout_price=450.00,
    )
    
    mock_history = pd.DataFrame(
        {
            "Open": [345.0, 348.0],
            "High": [350.0, 352.0],
            "Low": [340.0, 341.4],
            "Close": [347.0, 350.0],
            "Volume": [100000.0, 120000.0],
        },
        index=pd.date_range("2026-06-25", periods=2, freq="D")
    )
    
    import unittest.mock as mock
    with mock.patch("src.utils.db_loader.load_symbol_history_from_db", return_value=mock_history), \
         mock.patch("src.utils.data_loader.download_price_history", return_value=mock_history):
         
         worker = WatchlistAiWorker(
             watchlist_items=[item],
             db_engine=None,
             account_size=7200.0,
             risk_percent=0.01,
         )
         
         emitted_dfs = []
         def on_df_finished(df):
             emitted_dfs.append(df)
         worker.finished_analysis_df.connect(on_df_finished)
         worker.run()
         
         assert len(emitted_dfs) == 1
         df = emitted_dfs[0]
         assert isinstance(df, pd.DataFrame)
         assert "HUM" in df["symbol"].values


def test_environment_combos_synchronization():
    from PyQt5.QtWidgets import QComboBox
    from src.ui.main_window import MainWindow
    
    # Create QApplication instance if not present (PyQt requires it for widgets)
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    
    window = MainWindow.__new__(MainWindow)
    
    # Block signals during creation so we start clean
    window.watchlist_env_combo = QComboBox()
    window.watchlist_env_combo.blockSignals(True)
    window.watchlist_env_combo.addItems(["SIM", "PROD"])
    window.watchlist_env_combo.blockSignals(False)
    
    window.trade_kis_environment_combo = QComboBox()
    window.trade_kis_environment_combo.blockSignals(True)
    window.trade_kis_environment_combo.addItems(["SIM", "PROD"])
    window.trade_kis_environment_combo.blockSignals(False)
    
    # Mock calls
    populate_called = []
    apply_called = []
    calc_called = []
    review_called = []
    
    window.populate_trade_account_combo = lambda: populate_called.append(True)
    window.apply_cached_trade_account_size = lambda: apply_called.append(True)
    window.calculate_position_size = lambda show_warnings=False: calc_called.append(True)
    window.run_watchlist_ai_review = lambda: review_called.append(True)
    
    # Connect signals (same as in _setup_tabs, wrapped in lambdas due to uninitialized mock QObject)
    window.watchlist_env_combo.currentIndexChanged.connect(lambda idx: window.on_watchlist_env_changed(idx))
    window.watchlist_env_combo.currentIndexChanged.connect(lambda: window.run_watchlist_ai_review())
    window.trade_kis_environment_combo.currentTextChanged.connect(lambda env: window.on_trade_kis_environment_changed(env))

    
    # Verify initial state: both are index 0 ("SIM")
    assert window.watchlist_env_combo.currentText() == "SIM"
    assert window.trade_kis_environment_combo.currentText() == "SIM"
    
    # 1. Test changing watchlist_env_combo -> "PROD" (index 1)
    # This should sync trade_kis_environment_combo and run AI review.
    window.watchlist_env_combo.setCurrentIndex(1)
    
    assert window.trade_kis_environment_combo.currentText() == "PROD"
    assert len(populate_called) == 1
    assert len(apply_called) == 1
    assert len(calc_called) == 1
    assert len(review_called) == 1
    
    # Reset call counts
    populate_called.clear()
    apply_called.clear()
    calc_called.clear()
    review_called.clear()
    
    # 2. Test changing trade_kis_environment_combo -> "SIM" (index 0)
    # This should sync watchlist_env_combo and run AI review.
    window.trade_kis_environment_combo.setCurrentIndex(0)
    
    assert window.watchlist_env_combo.currentText() == "SIM"
    assert len(review_called) == 1
