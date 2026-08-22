from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import replace

import pandas as pd
import pytest
from sqlalchemy import MetaData, create_engine, func, select

from src.core.chart_fundamentals import ProfileStatus, StockProfile
from src.core.market_alignment import (
    ContextState,
    DailySeriesMetrics,
    MarketAlignmentSnapshot,
    calculate_fallback_market_rs,
    calculate_leadership_score,
    calculate_overall_context,
    context_state,
    daily_series_metrics,
    deterministic_percentile,
    evaluate_industry_context,
    evaluate_market_context,
    evaluate_sector_context,
    evaluate_segment_context,
    industry_peer_rankings,
    leadership_label,
    round_score,
)
from src.infrastructure.database.repositories import market_alignment as alignment_repo_module
from src.infrastructure.database.repositories.fundamentals import upsert_stock_profile
from src.infrastructure.database.repositories.market_alignment import (
    MarketAlignmentRepository,
)
from src.infrastructure.database.repositories.market_bars import save_symbol_history_to_db
from src.infrastructure.database.repositories.scanner import (
    save_scanner_metrics_snapshot_to_db,
    scanner_metrics_snapshot_date,
)
from src.infrastructure.database.schema import (
    _ensure_market_alignment_tables,
    _get_stock_market_alignment_daily_table,
)
from src.services.market_alignment import (
    MarketAlignmentService,
    refresh_market_alignment_to_db,
)


AS_OF = dt.date(2026, 8, 21)
NOW = dt.datetime(2026, 8, 22, 1, 2, 3, tzinfo=dt.timezone.utc)


def _history(scale=1.0, periods=270, end=AS_OF):
    dates = pd.bdate_range(end=end, periods=periods)
    close = pd.Series(
        [(100.0 + index * scale) for index in range(periods)],
        index=dates,
    )
    return pd.DataFrame(
        {
            "Open": close - 0.2,
            "High": close + 0.6,
            "Low": close - 0.7,
            "Close": close,
            "Adj Close": close,
            "Volume": [1_000_000.0] * periods,
        },
        index=dates,
    )


def _snapshot(symbol="AAPL", score=91.0, as_of=AS_OF):
    return MarketAlignmentSnapshot(
        symbol=symbol,
        as_of_date=as_of,
        feature_version="1.0",
        market_rs=94.0,
        market_rs_source="scanner_growth_rank_1m",
        industry_peer_rs=86.5,
        peer_basis="industry",
        peer_count=32,
        peer_group_id="semiconductors",
        peer_group_name="Semiconductors",
        leadership_score=score,
        leadership_label=leadership_label(score),
        market_state=ContextState.GREEN,
        market_conditions_passed=3,
        segment_name="Mega-Cap",
        segment_proxy="MGK",
        segment_state=ContextState.GREEN,
        segment_conditions_passed=3,
        sector_name="Technology",
        sector_proxy="XLK",
        sector_state=ContextState.GREEN,
        sector_conditions_passed=3,
        industry_name="Semiconductors",
        industry_proxy_or_index="SOXX",
        industry_state=ContextState.YELLOW,
        industry_conditions_passed=2,
        context_points=7.0,
        context_available_components=4,
        context_label="STRONG",
        is_provisional=False,
        classification_source="nasdaq",
        calculated_at=NOW,
        calculation_details={"metadata": {"data_status": "complete"}},
        market_cap=3_000_000_000_000.0,
        market_cap_as_of_date=AS_OF,
    )


def test_leadership_weight_rounding_and_boundaries_are_centralized():
    assert calculate_leadership_score(94, 87) == pytest.approx(91.2)
    assert round_score(79.5) == 80
    assert leadership_label(79.5) == "STRONG"
    assert leadership_label(79.49) == "MODERATE"
    assert leadership_label(59.49) == "WEAK"
    assert leadership_label(None) == "N/A"
    assert calculate_leadership_score(94, None) is None


def test_deterministic_ties_and_industry_then_sector_peer_fallback():
    ranks = deterministic_percentile({"B": 10, "A": 10, "C": 30})
    assert ranks == pytest.approx({"A": 50.0, "B": 50.0, "C": 100.0})

    market_rs = {f"S{index}": float(index) for index in range(1, 11)}
    classifications = {
        **{
            f"S{index}": {
                "industry_id": "chips",
                "industry_name": "Chips",
                "sector_id": "technology",
                "sector_name": "Technology",
            }
            for index in range(1, 6)
        },
        **{
            f"S{index}": {
                "industry_id": "software-a" if index < 8 else "software-b",
                "industry_name": "Small Software Group",
                "sector_id": "technology",
                "sector_name": "Technology",
            }
            for index in range(6, 11)
        },
    }
    peer = industry_peer_rankings(market_rs, classifications)

    assert peer["S5"]["peer_basis"] == "industry"
    assert peer["S5"]["peer_count"] == 5
    assert peer["S6"]["peer_basis"] == "sector_fallback"
    assert peer["S6"]["peer_count"] == 10


def test_insufficient_industry_and_sector_peers_leave_leadership_unavailable():
    market_rs = {"A": 90.0, "B": 80.0, "C": 70.0, "D": 60.0}
    classifications = {
        symbol: {
            "industry_id": "chips",
            "industry_name": "Chips",
            "sector_id": "technology",
            "sector_name": "Technology",
        }
        for symbol in market_rs
    }
    peer = industry_peer_rankings(market_rs, classifications)

    assert peer["A"]["peer_basis"] == "unavailable"
    assert peer["A"]["industry_peer_rs"] is None
    assert calculate_leadership_score(90, peer["A"]["industry_peer_rs"]) is None


@pytest.mark.parametrize(
    ("conditions", "expected"),
    [
        ((True, True, True), ContextState.GREEN),
        ((True, True, False), ContextState.YELLOW),
        ((True, False, False), ContextState.RED),
        ((None, False, False), ContextState.UNKNOWN),
    ],
)
def test_context_component_state_rules(conditions, expected):
    assert context_state(conditions) is expected


def test_market_rules_and_unknown_are_not_conflated_with_red():
    green = evaluate_market_context(
        DailySeriesMetrics(close=110, sma20=105, sma50=100, return_5d=0.01)
    )
    unknown = evaluate_market_context(
        DailySeriesMetrics(close=110, sma20=None, sma50=100, return_5d=0.01)
    )
    assert green.state is ContextState.GREEN
    assert green.conditions_passed == 3
    assert unknown.state is ContextState.UNKNOWN
    assert unknown.conditions_passed is None


def test_segment_sector_and_industry_rules_cover_green_yellow_red_and_unknown():
    metrics = DailySeriesMetrics(
        close=110,
        sma20=100,
        return_5d=0.03,
        return_20d=0.08,
    )
    assert evaluate_segment_context(metrics, 0.01).state is ContextState.GREEN
    assert evaluate_segment_context(metrics, 0.04).state is ContextState.YELLOW
    assert evaluate_segment_context(
        replace(metrics, return_5d=-0.01), 0.01
    ).state is ContextState.RED
    assert evaluate_segment_context(
        replace(metrics, sma20=None), 0.01
    ).state is ContextState.UNKNOWN

    assert evaluate_sector_context(metrics, 0.01, 80).state is ContextState.GREEN
    assert evaluate_sector_context(metrics, 0.04, 80).state is ContextState.YELLOW
    assert evaluate_sector_context(
        replace(metrics, close=90), 0.04, 60
    ).state is ContextState.RED
    assert evaluate_sector_context(metrics, 0.01, None).state is ContextState.UNKNOWN

    assert evaluate_industry_context(metrics, 0.01, 80).state is ContextState.GREEN
    assert evaluate_industry_context(metrics, 0.04, 80).state is ContextState.YELLOW
    assert evaluate_industry_context(
        replace(metrics, close=90), 0.04, 60
    ).state is ContextState.RED
    assert evaluate_industry_context(metrics, None, 80).state is ContextState.UNKNOWN


def test_overall_context_boundaries_red_cap_and_provisional_normalization():
    assert calculate_overall_context(
        ContextState.GREEN,
        ContextState.GREEN,
        ContextState.GREEN,
        ContextState.YELLOW,
    ).label == "STRONG"
    assert calculate_overall_context(
        ContextState.RED,
        ContextState.GREEN,
        ContextState.GREEN,
        ContextState.GREEN,
    ).label == "MIXED"
    provisional = calculate_overall_context(
        ContextState.GREEN,
        ContextState.GREEN,
        ContextState.YELLOW,
        ContextState.UNKNOWN,
    )
    assert provisional.is_provisional is True
    assert provisional.available_components == 3
    assert provisional.points == 5
    assert provisional.normalized_points == pytest.approx(20 / 3)
    assert provisional.label == "SUPPORTIVE"
    unavailable = calculate_overall_context(
        ContextState.GREEN,
        ContextState.UNKNOWN,
        ContextState.UNKNOWN,
        ContextState.RED,
    )
    assert unavailable.label == "UNKNOWN"
    assert unavailable.points is None


def test_daily_features_use_one_completed_date_deduplicate_and_scale_once():
    history = _history(periods=60)
    future = history.iloc[[-1]].copy()
    future.index = pd.DatetimeIndex([pd.Timestamp(AS_OF) + pd.Timedelta(days=3)])
    future["Close"] = 10_000
    future["Adj Close"] = 10_000
    duplicate = history.iloc[[-1]].copy()
    combined = pd.concat([history, duplicate, future])

    metrics = daily_series_metrics(combined, AS_OF)

    assert metrics.source_date == AS_OF
    assert metrics.close == pytest.approx(history["Close"].iloc[-1])
    expected_return = history["Close"].iloc[-1] / history["Close"].iloc[-6] - 1
    assert metrics.return_5d == pytest.approx(expected_return)
    assert metrics.return_5d < 1  # decimal internally; UI alone converts to percent


def test_daily_features_keep_adjusted_close_and_moving_averages_on_same_basis():
    history = _history(periods=60)
    history["Adj Close"] = history["Close"] * 0.5

    metrics = daily_series_metrics(history, AS_OF)

    assert metrics.close == pytest.approx(history["Adj Close"].iloc[-1])
    assert metrics.sma20 == pytest.approx(history["Adj Close"].iloc[-20:].mean())


def test_fallback_market_rs_requires_full_history_and_is_cross_sectional():
    histories = {
        "FAST": _history(scale=1.2),
        "MID": _history(scale=0.6),
        "SLOW": _history(scale=0.2),
        "SHORT": _history(scale=2.0, periods=100),
    }
    scores, components = calculate_fallback_market_rs(
        histories, list(histories), AS_OF
    )

    assert scores["FAST"] > scores["MID"] > scores["SLOW"]
    assert "SHORT" not in scores
    assert set(components["FAST"]) == {
        "return_63_percentile",
        "return_126_percentile",
        "return_252_percentile",
    }


def test_repository_upsert_is_idempotent_and_lookup_requires_published_manifest():
    engine = create_engine("sqlite:///:memory:", future=True)
    repository = MarketAlignmentRepository(engine)
    snapshot = _snapshot()

    assert repository.publish_batch([snapshot], input_fingerprint="a", stats={}) == 1
    assert repository.publish_batch([snapshot], input_fingerprint="a", stats={}) == 1
    loaded = repository.get_latest_market_alignment("aapl")
    assert loaded is not None
    assert loaded.symbol == "AAPL"
    assert loaded.leadership_score == 91.0
    assert loaded.market_state is ContextState.GREEN

    table = _get_stock_market_alignment_daily_table(MetaData())
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(table)).scalar_one() == 1


def test_chart_lookup_treats_unprovisioned_optional_schema_as_missing_data(caplog):
    engine = create_engine("sqlite:///:memory:", future=True)

    with caplog.at_level(logging.WARNING):
        snapshot = MarketAlignmentService(engine).get_latest_market_alignment(
            "AAPL", expected_date=AS_OF
        )

    assert snapshot is None
    assert "market_alignment_lookup_failed" not in caplog.text


def test_failed_force_publication_rolls_back_and_preserves_last_successful(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    repository = MarketAlignmentRepository(engine)
    original = _snapshot(score=70.0)
    repository.publish_batch([original], input_fingerprint="old", stats={})

    real_upsert = alignment_repo_module._execute_bulk_upsert

    def fail_manifest(conn, table, records, keys, dialect):
        if table.name == "market_alignment_batches":
            raise RuntimeError("manifest write failed")
        return real_upsert(conn, table, records, keys, dialect)

    monkeypatch.setattr(alignment_repo_module, "_execute_bulk_upsert", fail_manifest)
    with pytest.raises(RuntimeError, match="manifest write failed"):
        repository.publish_batch(
            [replace(original, leadership_score=99.0, leadership_label="STRONG")],
            input_fingerprint="new",
            stats={},
        )

    assert repository.get_latest_market_alignment("AAPL").leadership_score == 70.0


def test_chart_lookup_marks_stale_without_calculation_or_schema_creation():
    engine = create_engine("sqlite:///:memory:", future=True)
    _ensure_market_alignment_tables(engine)
    MarketAlignmentRepository(engine).publish_batch(
        [_snapshot(as_of=dt.date(2026, 8, 20))],
        input_fingerprint="x",
        stats={},
    )

    loaded = MarketAlignmentService(engine).get_latest_market_alignment(
        "AAPL", expected_date=AS_OF
    )

    assert loaded is not None
    assert loaded.is_stale is True


def test_daily_batch_reuses_scanner_rank_publishes_and_skips_same_session(tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    symbols = [f"S{index}" for index in range(1, 7)]
    for index, symbol in enumerate(symbols, start=1):
        assert save_symbol_history_to_db(symbol, _history(scale=0.1 * index), engine)
        upsert_stock_profile(
            engine,
            StockProfile(
                symbol=symbol,
                company_name=symbol,
                source="nasdaq",
                last_checked_at=NOW,
                updated_at=NOW,
                sector_name="Technology",
                industry_name="Semiconductors",
                market_cap=300_000_000_000.0,
                market_cap_as_of_date=AS_OF,
                profile_status=ProfileStatus.OK,
            ),
        )
    for ticker, scale in {
        "SPY": 0.2,
        "MGK": 0.3,
        "XLK": 0.4,
        "XLF": 0.1,
        "SOXX": 0.5,
    }.items():
        assert save_symbol_history_to_db(ticker, _history(scale=scale), engine)

    metric_date = scanner_metrics_snapshot_date(AS_OF)
    metrics = [
        {
            "symbol": symbol,
            "growth_rank_1m": float(60 + index * 5),
            "volume": 1_000_000,
            "dollar_volume": 100_000_000,
            "price_history_days": 270,
        }
        for index, symbol in enumerate(symbols)
    ]
    assert save_scanner_metrics_snapshot_to_db(
        metrics, metric_date, "scanner", engine
    ) == symbols

    config_path = tmp_path / "pulse.json"
    config_path.write_text(
        json.dumps(
            {
                "instruments": [
                    {"section": "market_segments", "display_name": "Mega-Cap Growth", "ticker": "MGK", "display_order": 1, "is_active": True},
                    {"section": "sectors", "display_name": "Technology", "ticker": "XLK", "display_order": 1, "is_active": True},
                    {"section": "sectors", "display_name": "Financials", "ticker": "XLF", "display_order": 2, "is_active": True},
                    {"section": "industries_themes", "display_name": "Semiconductors", "ticker": "SOXX", "display_order": 1, "is_active": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    first = refresh_market_alignment_to_db(
        symbols,
        engine,
        as_of_date=AS_OF,
        market_pulse_config_path=config_path,
    )
    second = refresh_market_alignment_to_db(
        symbols,
        engine,
        as_of_date=AS_OF,
        market_pulse_config_path=config_path,
    )
    forced = refresh_market_alignment_to_db(
        symbols,
        engine,
        as_of_date=AS_OF,
        market_pulse_config_path=config_path,
        force=True,
    )
    snapshot = MarketAlignmentRepository(engine).get_latest_market_alignment("S6")

    assert first["published_rows"] == 6
    assert second["skipped"] is True
    assert forced["skipped"] is False
    assert snapshot.market_rs == 85.0
    assert snapshot.market_rs_source == "scanner_growth_rank_1m"
    assert snapshot.peer_basis == "industry"
    assert snapshot.segment_proxy == "MGK"
    assert snapshot.sector_proxy == "XLK"
    assert snapshot.industry_proxy_or_index == "SOXX"
    assert snapshot.context_available_components == 4
    assert snapshot.calculation_details["metadata"]["as_of_date"] == AS_OF.isoformat()
