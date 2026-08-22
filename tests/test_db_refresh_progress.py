import numpy as np
import pandas as pd
from sqlalchemy import Boolean, MetaData, create_engine, false
from sqlalchemy.dialects import mysql, sqlite

import src.utils.data_loader as data_loader
import src.utils.db_loader as db_loader
from src.infrastructure.database.repositories import chart_indicators, scanner
from src.infrastructure.database.schema import (_get_scanner_metrics_table,
                                                _get_stock_profiles_table)
from src.ui.filter_catalog import FILTER_CATALOG


def test_chart_indicator_refresh_logs_progress(monkeypatch):
    logs = []
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [11.0, 12.0],
            "Low": [9.0, 10.0],
            "Close": [10.5, 11.5],
            "Volume": [1000.0, 1200.0],
        },
        index=[pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")],
    )
    monkeypatch.setattr(
        chart_indicators,
        "load_universe_history_from_db",
        lambda tickers, engine, start=None, end=None, interval="1d": {"SPY": history, "AAPL": history},
    )
    monkeypatch.setattr(
        chart_indicators,
        "get_chart_indicator_refresh_plan",
        lambda *args, **kwargs: {
            "AAPL": pd.Timestamp("2026-01-05").to_pydatetime(),
            "BAD": pd.Timestamp("2026-01-05").to_pydatetime(),
        },
    )
    monkeypatch.setattr(chart_indicators, "save_chart_indicators_batch_to_db", lambda records, engine: len(records))

    engine = create_engine("sqlite:///:memory:", future=True)
    updated = db_loader.refresh_chart_indicators_to_db(
        ["SPY", "AAPL", "BAD"],
        engine=engine,
        log_callback=logs.append,
    )

    assert updated == ["AAPL"]
    assert any("Calculating chart indicators" in message for message in logs)
    assert any("Chart indicators progress" in message for message in logs)
    assert any("BAD: unable to calculate chart indicators" in message for message in logs)


def test_scanner_metrics_refresh_logs_calculate_and_save_progress(monkeypatch):
    logs = []
    history = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [1000.0],
        },
        index=[pd.Timestamp("2026-01-05")],
    )

    monkeypatch.setattr(
        scanner,
        "load_universe_history_from_db",
        lambda tickers, engine, start=None: {"SPY": history, "AAPL": history, "MSFT": history},
    )
    monkeypatch.setattr(
        data_loader,
        "compute_stock_metrics",
        lambda symbol, symbol_history, spy_history=None: {"symbol": symbol, "return_1m": 1.0, "return_3m": 1.0},
    )
    snapshot_date = pd.Timestamp("2026-01-05").to_pydatetime()
    monkeypatch.setattr(scanner, "scanner_metrics_snapshot_date", lambda: snapshot_date)
    monkeypatch.setattr(
        scanner,
        "get_price_history_watermarks",
        lambda *args, **kwargs: {
            "SPY": (snapshot_date, 1),
            "AAPL": (snapshot_date, 1),
            "MSFT": (snapshot_date, 1),
        },
    )
    cache_checks = iter([False, True])
    monkeypatch.setattr(
        scanner,
        "is_scanner_metrics_snapshot_current",
        lambda *args, **kwargs: next(cache_checks),
    )
    monkeypatch.setattr(
        scanner,
        "save_scanner_metrics_snapshot_to_db",
        lambda metrics_list, date, fingerprint, engine, **kwargs: [
            item["symbol"] for item in metrics_list
        ],
    )

    updated = db_loader.refresh_scanner_metrics_to_db(
        ["AAPL", "MSFT"],
        engine=object(),
        log_callback=logs.append,
    )

    assert updated == ["AAPL", "MSFT"]
    assert any("Calculating scanner metrics" in message for message in logs)
    assert any("Scanner metrics progress" in message for message in logs)
    assert any("Saving scanner metrics" in message for message in logs)
    assert any("Scanner metrics save progress" in message for message in logs)


def test_scanner_metrics_batch_save_accepts_numpy_scalars():
    engine = create_engine("sqlite:///:memory:", future=True)
    date = pd.Timestamp("2026-01-05").to_pydatetime()

    saved = db_loader.save_scanner_metrics_batch_to_db(
        [
            {
                "symbol": "AAPL",
                "price": np.float64(123.45),
                "volume": np.int64(1000),
                "above_sma_20": np.bool_(True),
            }
        ],
        date,
        engine,
    )
    loaded = db_loader.load_scanner_metrics_from_db(["AAPL"], engine, date)

    assert saved == ["AAPL"]
    assert loaded[0]["price"] == 123.45
    assert loaded[0]["volume"] == 1000
    assert loaded[0]["above_sma_20"] is True


def test_scanner_query_filters_in_sql_and_returns_sequential_funnel_counts():
    engine = create_engine("sqlite:///:memory:", future=True)
    date = pd.Timestamp("2026-01-05").to_pydatetime()
    metrics = [
        {"symbol": "PASS", "price_history_days": 20, "volume": 200_000, "adr_20": 4.0},
        {"symbol": "LOW_ADR", "price_history_days": 20, "volume": 150_000, "adr_20": 2.0},
        {"symbol": "LOW_VOLUME", "price_history_days": 20, "volume": 50_000, "adr_20": 5.0},
        {"symbol": "NO_HISTORY", "price_history_days": 0, "volume": 500_000, "adr_20": 6.0},
    ]
    assert scanner.save_scanner_metrics_batch_to_db(metrics, date, engine) == [
        "PASS",
        "LOW_ADR",
        "LOW_VOLUME",
        "NO_HISTORY",
    ]

    results, funnel = scanner.query_scanner_metrics_with_funnel(
        [item["symbol"] for item in metrics],
        engine,
        [
            {"attribute": "volume", "operator": ">=", "threshold": 100_000},
            {"attribute": "adr_20", "operator": ">=", "threshold": 3.0},
        ],
        date=date,
    )

    assert [item["symbol"] for item in results] == ["PASS"]
    assert funnel == {"universe_count": 3, "rule_counts": [2, 1]}


def test_scanner_query_rejects_unknown_columns_instead_of_interpolating_sql():
    engine = create_engine("sqlite:///:memory:", future=True)
    date = pd.Timestamp("2026-01-05").to_pydatetime()
    assert scanner.save_scanner_metrics_batch_to_db(
        [{"symbol": "AAPL", "price_history_days": 20, "volume": 200_000}],
        date,
        engine,
    ) == ["AAPL"]

    results, funnel = scanner.query_scanner_metrics_with_funnel(
        ["AAPL"],
        engine,
        [{"attribute": "volume) OR 1=1 --", "operator": ">=", "threshold": 0}],
        date=date,
    )

    assert results == []
    assert funnel == {"universe_count": 1, "rule_counts": [0]}


def test_every_catalog_filter_compiles_for_sqlite_and_mysql():
    metadata = MetaData()
    metrics_table = _get_scanner_metrics_table(metadata)
    profiles_table = _get_stock_profiles_table(metadata)
    overrides = {"name": profiles_table.c.company_name}

    assert len(FILTER_CATALOG) == 51
    for _category, attribute, *_rest in FILTER_CATALOG:
        if attribute == "symbol":
            threshold = "AAPL"
        elif attribute == "name":
            threshold = "Apple Inc."
        elif attribute in metrics_table.columns and isinstance(
            metrics_table.c[attribute].type, Boolean
        ):
            threshold = "True"
        else:
            threshold = 1

        for operator in (">", "<", "==", ">=", "<=", "!="):
            expression = scanner._scanner_rule_expression(
                metrics_table,
                {
                    "attribute": attribute,
                    "operator": operator,
                    "threshold": threshold,
                },
                overrides,
            )
            assert not expression.compare(false()), (attribute, operator)
            str(expression.compile(dialect=sqlite.dialect()))
            str(expression.compile(dialect=mysql.dialect()))


def test_scanner_name_filter_joins_profiles_and_returns_company_name():
    engine = create_engine("sqlite:///:memory:", future=True)
    date = pd.Timestamp("2026-01-05").to_pydatetime()
    metrics = [
        {
            "symbol": "AAPL",
            "price_history_days": 20,
            "volume_expansion": 2.0,
        },
        {
            "symbol": "MSFT",
            "price_history_days": 20,
            "volume_expansion": 0.8,
        },
    ]
    assert scanner.save_scanner_metrics_batch_to_db(metrics, date, engine) == [
        "AAPL",
        "MSFT",
    ]

    metadata = MetaData()
    profiles_table = _get_stock_profiles_table(metadata)
    metadata.create_all(engine)
    profile_rows = []
    for symbol, company_name in (("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corp.")):
        profile_rows.append(
            {
                "symbol": symbol,
                "company_name": company_name,
                "profile_status": "complete",
                "source": "test",
                "last_checked_at": date,
                "created_at": date,
                "updated_at": date,
            }
        )
    with engine.begin() as connection:
        connection.execute(profiles_table.insert(), profile_rows)

    results, funnel = scanner.query_scanner_metrics_with_funnel(
        ["AAPL", "MSFT"],
        engine,
        [
            {"attribute": "name", "operator": "==", "threshold": "Apple Inc."},
            {"attribute": "volume_expansion", "operator": ">=", "threshold": 1.5},
        ],
        date=date,
    )

    assert [(item["symbol"], item["name"]) for item in results] == [
        ("AAPL", "Apple Inc.")
    ]
    assert funnel == {"universe_count": 2, "rule_counts": [1, 1]}


def test_boolean_filter_operators_preserve_python_comparison_semantics():
    engine = create_engine("sqlite:///:memory:", future=True)
    date = pd.Timestamp("2026-01-05").to_pydatetime()
    assert scanner.save_scanner_metrics_batch_to_db(
        [
            {"symbol": "FALSE", "price_history_days": 20, "above_sma_20": False},
            {"symbol": "TRUE", "price_history_days": 20, "above_sma_20": True},
        ],
        date,
        engine,
    ) == ["FALSE", "TRUE"]
    expected = {
        ">": set(),
        "<": {"FALSE"},
        "==": {"TRUE"},
        ">=": {"TRUE"},
        "<=": {"FALSE", "TRUE"},
        "!=": {"FALSE"},
    }

    for operator, expected_symbols in expected.items():
        results, funnel = scanner.query_scanner_metrics_with_funnel(
            ["FALSE", "TRUE"],
            engine,
            [
                {
                    "attribute": "above_sma_20",
                    "operator": operator,
                    "threshold": "True",
                }
            ],
            date=date,
        )
        assert {item["symbol"] for item in results} == expected_symbols
        assert funnel == {
            "universe_count": 2,
            "rule_counts": [len(expected_symbols)],
        }
