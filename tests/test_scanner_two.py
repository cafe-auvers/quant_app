import datetime as dt

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Column, MetaData, Table, create_engine, inspect, select

from src.core.breakout_replay import rank_and_scan
from src.core.scanner import add_momentum_ranks
from src.infrastructure.database.repositories.scanner import (
    query_scanner_metrics_with_funnel,
    save_scanner_metrics_batch_to_db,
)
from src.infrastructure.database.schema import (
    _ensure_price_history_table,
    _ensure_scanner_metrics_table,
    _get_scanner_metrics_table,
)
from src.ui.filter_catalog import DEFAULT_SCANNER_SETUPS
from src.utils.data_loader import compute_stock_metrics


def test_scanner_one_keeps_its_original_conditions():
    assert DEFAULT_SCANNER_SETUPS["Setup 1"]["rules"] == [
        {"attribute": "volume", "operator": ">=", "threshold": 40000.0},
        {"attribute": "dollar_volume", "operator": ">=", "threshold": 35000.0},
        {"attribute": "adr_20", "operator": ">=", "threshold": 2.4},
        {"attribute": "growth_rank_1m", "operator": ">=", "threshold": 97.04},
        {"attribute": "trend_intensity", "operator": ">=", "threshold": 90.0},
    ]


def candidates():
    rows = [{"symbol": f"S{i}", "price_history_days": 127,
             "return_1m": i, "return_3m": i, "return_6m": i,
             "adr_20_high_low": 2.4, "avg_dollar_volume_20d": 3_000_000,
             "adr_20": 0, "volume": 0, "dollar_volume": 0, "trend_intensity": 0}
            for i in range(100)]
    rows[0]["return_1m"] = 200
    rows[1]["return_3m"] = 200
    rows[2]["return_6m"] = 200
    return rows


def test_scanner_two_accepts_leadership_in_any_period_without_legacy_gates():
    rows = candidates()
    results = rank_and_scan(rows, DEFAULT_SCANNER_SETUPS["Setup 2"]["rules"])
    symbols = {row["symbol"] for row in results}
    assert {"S0", "S1", "S2"} <= symbols
    assert "S50" not in symbols
    assert rows[2]["momentum_rank_6m"] == 100
    assert rows[2]["momentum_rank_1m"] < 98


@pytest.mark.parametrize("field,value", [("adr_20_high_low", 2.399),
                                        ("avg_dollar_volume_20d", 2_999_999)])
def test_scanner_two_applies_liquidity_and_adr_after_universe_ranking(field, value):
    rows = candidates()
    rows[0][field] = value
    results = rank_and_scan(rows, DEFAULT_SCANNER_SETUPS["Setup 2"]["rules"])
    assert "S0" not in {row["symbol"] for row in results}
    assert rows[0]["momentum_rank_1m"] == 100
    assert rows[99]["momentum_rank_1m"] == 99


def test_short_histories_and_missing_returns_do_not_become_leaders():
    rows = [{"price_history_days": 21, "return_1m": 1000, "return_3m": 1000,
             "return_6m": 1000, "growth_rank_1m": 17},
            {"price_history_days": 22, "return_1m": 5, "return_3m": 1000,
             "return_6m": 1000, "growth_rank_1m": 18},
            {"price_history_days": 127, "return_1m": 3, "return_3m": None,
             "return_6m": np.nan, "growth_rank_1m": 19}]
    add_momentum_ranks(rows)
    assert rows[0]["momentum_rank_any"] is None
    assert rows[1]["momentum_rank_1m"] == 100
    assert rows[1]["momentum_rank_3m"] is None
    assert rows[2]["momentum_rank_6m"] is None
    assert [row["growth_rank_1m"] for row in rows] == [17, 18, 19]


def test_flat_return_ties_do_not_put_the_whole_universe_in_top_two_percent():
    rows = [{"return_1m": 0, "return_3m": 0, "return_6m": 0} for _ in range(100)]
    rows[0]["return_1m"] = 5
    add_momentum_ranks(rows)
    assert [i for i, row in enumerate(rows) if row["momentum_rank_any"] >= 98] == [0]


def test_high_low_adr_needs_twenty_complete_sessions_and_preserves_legacy_adr():
    bars = pd.DataFrame({"Open": 101, "High": 104, "Low": 100,
                         "Close": 102, "Volume": 40_000},
                        index=pd.bdate_range("2025-01-02", periods=22))
    metrics = compute_stock_metrics("A", bars)
    assert metrics["adr_20_high_low"] == pytest.approx(4)
    assert metrics["adr_20"] == pytest.approx(4 / 102 * 100)
    assert metrics["avg_dollar_volume_20d"] == 4_080_000
    assert compute_stock_metrics("A", bars.iloc[:19])["adr_20_high_low"] is None
    invalid = bars.copy()
    invalid.iloc[-1, invalid.columns.get_loc("Low")] = 0
    assert compute_stock_metrics("A", invalid)["adr_20_high_low"] is None


def test_scanner_two_metrics_are_saved_and_filtered_in_database():
    engine = create_engine("sqlite://")
    rows = candidates()
    add_momentum_ranks(rows)
    day = dt.datetime(2025, 1, 2)
    assert len(save_scanner_metrics_batch_to_db(rows, day, engine)) == 100
    results, funnel = query_scanner_metrics_with_funnel(
        [row["symbol"] for row in rows], engine, DEFAULT_SCANNER_SETUPS["Setup 2"]["rules"], day)
    assert {"S0", "S1", "S2"} <= {row["symbol"] for row in results}
    assert funnel["universe_count"] == 100
    engine.dispose()


def test_existing_scanner_cache_migrates_without_changing_legacy_rows():
    engine = create_engine("sqlite://")
    new_table = _get_scanner_metrics_table(MetaData())
    additions = {"adr_20_high_low", "ti65", "momentum_rank_1m", "momentum_rank_3m",
                 "momentum_rank_6m", "momentum_rank_any"}
    old_metadata = MetaData()
    old_table = Table("scanner_metrics", old_metadata, *[
        Column(column.name, column.type, primary_key=column.primary_key,
               nullable=column.nullable) for column in new_table.columns
        if column.name not in additions])
    old_metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(old_table.insert().values(symbol="A", date=dt.datetime(2025, 1, 2),
                    adr_20=3.5, updated_at=dt.datetime(2025, 1, 3)))
    table = _ensure_scanner_metrics_table(engine)
    _ensure_scanner_metrics_table(engine)
    assert additions <= {column["name"] for column in inspect(engine).get_columns(table.name)}
    with engine.connect() as conn:
        row = conn.execute(select(table)).mappings().one()
    assert row["adr_20"] == 3.5 and row["adr_20_high_low"] is None
    engine.dispose()


def test_desktop_loads_the_same_scanner_two_rules():
    from src.ui.main_window import MainWindow
    from src.ui.mixins.scanner_mixin import ScannerMixin

    setups = MainWindow._normalize_scanner_setups({"setups": DEFAULT_SCANNER_SETUPS})
    for name in ("Setup 1", "Setup 2"):
        assert ScannerMixin._rules_for_scanner_setup(setups[name]) == DEFAULT_SCANNER_SETUPS[name]["rules"]


@pytest.mark.parametrize("explicit_rules", [False, True])
def test_desktop_upgrades_the_old_saved_scanner_two_default(explicit_rules):
    from src.ui.main_window import MainWindow

    legacy = {"min_volume": 250_000, "min_dollar_volume": 5_000_000,
              "min_adr": 3, "min_growth_rank": 95, "min_trend_intensity": 80}
    if explicit_rules:
        legacy["rules"] = [
            {"attribute": name, "operator": ">=", "threshold": str(value)}
            for name, value in (("trend_intensity", 80), ("growth_rank_1m", 95),
                                ("adr_20", 3), ("dollar_volume", 5_000_000),
                                ("volume", 250_000))]
    setups = MainWindow._normalize_scanner_setups({"setups": {
        "Setup 1": DEFAULT_SCANNER_SETUPS["Setup 1"], "Setup 2": legacy}})
    assert setups["Setup 2"]["rules"] == DEFAULT_SCANNER_SETUPS["Setup 2"]["rules"]
    assert setups["Setup 1"]["rules"] == DEFAULT_SCANNER_SETUPS["Setup 1"]["rules"]


def test_desktop_preserves_user_edited_scanner_two_rules():
    from src.ui.main_window import MainWindow

    rules = [{"attribute": "volume", "operator": ">=", "threshold": 300_000}]
    setups = MainWindow._normalize_scanner_setups({"setups": {"Setup 2": {"rules": rules}}})
    assert setups["Setup 2"]["rules"] == rules


@pytest.mark.parametrize("spy_fallback", [False, True])
def test_desktop_scanner_two_upgrades_local_cache_once_and_preserves_snapshot_date(monkeypatch, spy_fallback):
    import src.infrastructure.database.repositories.scanner as repository
    from src.ui.workers import ScannerWorker

    engine = create_engine("sqlite://")
    daily = _ensure_price_history_table(engine)
    snapshot = dt.datetime(2025, 1, 7)
    monkeypatch.setattr(repository, "scanner_metrics_snapshot_date", lambda: dt.datetime(2025, 1, 10))
    frames = {}
    records = []
    dates = pd.bdate_range(end=snapshot, periods=150)
    for symbol, increment in (("A", 1), ("B", .1), ("SPY", .05)):
        close = 100 + np.arange(len(dates)) * increment
        frames[symbol] = pd.DataFrame({"Open": close, "High": close * 1.04,
            "Low": close * .96, "Close": close, "Volume": 100_000}, index=dates)
        for stamp, bar in frames[symbol].iterrows():
            records.append({"symbol": symbol, "date": stamp.to_pydatetime(), "interval": "1d",
                "updated_at": snapshot, **{name.lower(): float(bar[name])
                    for name in ("Open", "High", "Low", "Close", "Volume")}})
        for stamp in pd.bdate_range("2025-01-08", "2025-01-10"):
            records.append({"symbol": symbol, "date": stamp.to_pydatetime(), "interval": "1d",
                "updated_at": stamp.to_pydatetime(), "open": 10_000, "high": 11_000,
                "low": 9000, "close": 10_000, "volume": 100_000})
    with engine.begin() as conn:
        conn.execute(daily.insert(), records)
    legacy = [compute_stock_metrics(symbol, frames[symbol], spy_history=frames["SPY"])
              for symbol in ("A", "B")]
    for metrics in legacy:
        metrics.pop("adr_20_high_low")
        metrics.pop("ti65")
    assert repository.save_scanner_metrics_snapshot_to_db(legacy, snapshot, "legacy-cache", engine) == ["A", "B"]
    if spy_fallback:
        load_universe = repository.load_universe_history_from_db
        load_symbol = repository.load_symbol_history_from_db

        def load_without_spy(*args, **kwargs):
            histories = load_universe(*args, **kwargs)
            histories.pop("SPY", None)
            return histories

        def load_bounded_spy(*args, **kwargs):
            assert kwargs["start"] == snapshot - dt.timedelta(days=380)
            assert kwargs["end"] == snapshot + dt.timedelta(days=1, microseconds=-1)
            return load_symbol(*args, **kwargs)

        monkeypatch.setattr(repository, "load_universe_history_from_db", load_without_spy)
        monkeypatch.setattr(repository, "load_symbol_history_from_db", load_bounded_spy)
    worker = ScannerWorker(tickers=["A", "B"], engine=engine, min_volume=0,
        min_dollar_volume=0, min_adr=0, min_growth_rank=0, min_trend_intensity=0,
        scanner_rules_by_setup={"Setup 2": DEFAULT_SCANNER_SETUPS["Setup 2"]["rules"]})
    completions, errors, logs = [], [], []
    worker.finished_scan.connect(lambda rows, payload: completions.append(payload))
    worker.error_occurred.connect(errors.append)
    worker.log_message.connect(logs.append)
    monkeypatch.setattr("src.utils.data_loader.yf.download", lambda *a, **k: pytest.fail("Do not fetch Yahoo data"))
    worker.run()
    assert errors == [] and len(completions) == 1
    results = completions[0]["results_by_setup"]["Setup 2"]
    assert [row["symbol"] for row in results] == ["A"]
    assert results[0]["price"] == 249  # Later cached bars were excluded.
    assert results[0]["adr_20_high_low"] == pytest.approx(100 * (1.04 / .96 - 1))
    assert completions[0]["funnels_by_setup"]["Setup 2"]["snapshot_date"] == "2025-01-07"
    assert any("Preparing scanner2" in message for message in logs)
    monkeypatch.setattr(repository, "load_universe_history_from_db", lambda *a, **k: pytest.fail("Reuse the saved metrics"))
    monkeypatch.setattr("src.utils.data_loader.compute_stock_metrics", lambda *a, **k: pytest.fail("Do not recalculate"))
    worker.run()
    assert errors == [] and completions[1] == completions[0]
    assert len([message for message in logs if "Preparing scanner2" in message]) == 1
    engine.dispose()


def test_desktop_scanner_two_reports_missing_cache_instead_of_empty_matches(monkeypatch):
    from src.ui.workers import ScannerWorker

    engine = create_engine("sqlite://")
    _ensure_price_history_table(engine)
    worker = ScannerWorker(tickers=["A"], engine=engine, min_volume=0,
        min_dollar_volume=0, min_adr=0, min_growth_rank=0, min_trend_intensity=0,
        scanner_rules_by_setup={"Setup 2": DEFAULT_SCANNER_SETUPS["Setup 2"]["rules"]})
    completions, errors = [], []
    worker.finished_scan.connect(lambda rows, payload: completions.append(payload))
    worker.error_occurred.connect(errors.append)
    monkeypatch.setattr("src.utils.data_loader.yf.download", lambda *a, **k: pytest.fail("Do not fetch Yahoo data"))
    worker.run()
    assert completions == []
    assert len(errors) == 1 and "local daily cache" in errors[0]
    engine.dispose()
