import datetime as dt
import json
import threading

import pandas as pd
import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError

from src.core.market_pulse import (
    INDUSTRIES_THEMES,
    MARKET_SEGMENTS,
    SECTORS,
    MarketPulseInstrument,
    MarketPulseRow,
    MarketPulseSnapshot,
    calculate_market_pulse_metrics,
    calculate_relative_strength_vs_benchmark,
    rank_symbols_by_relative_strength,
    rank_market_pulse_rows,
    snapshot_to_dict,
)
from src.infrastructure.database.repositories.market_pulse import (
    MarketPulseSnapshotRepository,
)
from src.infrastructure.database.schema import (
    _get_market_pulse_instruments_table,
    _get_market_pulse_snapshots_table,
)
from src.services.market_pulse import (
    MarketPulseComponentsBatch,
    MarketPulseHistoryBatch,
    MarketPulseRefreshError,
    MarketPulseRefreshInProgress,
    MarketPulseService,
    YFinanceMarketPulseProvider,
)


AS_OF = dt.date(2026, 8, 21)


def _history(values, *, end=AS_OF, adjusted=None):
    dates = pd.bdate_range(end=end, periods=len(values))
    frame = pd.DataFrame({"Close": values}, index=dates)
    if adjusted is not None:
        frame["Adj Close"] = adjusted
    return frame


def _row(section, ticker, daily, **kwargs):
    return MarketPulseRow(
        section=section,
        display_name=kwargs.pop("display_name", ticker),
        ticker=ticker,
        display_order=kwargs.pop("display_order", 1),
        rank=kwargs.pop("rank", 0),
        close=kwargs.pop("close", 100.0),
        daily_return=daily,
        weekly_return=kwargs.pop("weekly_return", daily),
        monthly_return=kwargs.pop("monthly_return", daily),
        pct_above_52w_low=kwargs.pop("pct_above_52w_low", 0.2),
        pct_below_52w_high=kwargs.pop("pct_below_52w_high", -0.1),
        source_session_date=kwargs.pop("source_session_date", AS_OF),
        **kwargs,
    )


def _snapshot(rows, as_of=AS_OF):
    return MarketPulseSnapshot(
        as_of_date=as_of,
        refreshed_at=dt.datetime(2026, 8, 22, tzinfo=dt.timezone.utc),
        source="test_adjusted",
        rows=rank_market_pulse_rows(rows),
        failures={},
    )


def _write_config(path, instruments):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "instruments": [
                    {
                        "section": item.section,
                        "display_name": item.display_name,
                        "ticker": item.ticker,
                        "display_order": item.display_order,
                        "is_active": item.is_active,
                    }
                    for item in instruments
                ],
            }
        ),
        encoding="utf-8",
    )


def test_calculates_session_returns_and_52_week_distances_as_decimals():
    values = [100.0 + index for index in range(252)]
    metrics = calculate_market_pulse_metrics(_history(values), AS_OF)

    assert metrics.close == 351.0
    assert metrics.daily_return == pytest.approx(351 / 350 - 1)
    assert metrics.weekly_return == pytest.approx(351 / 346 - 1)
    assert metrics.monthly_return == pytest.approx(351 / 330 - 1)
    assert metrics.pct_above_52w_low == pytest.approx(351 / 100 - 1)
    assert metrics.pct_below_52w_high == pytest.approx(0.0)
    # Internally 1.27% is 0.0127; there is no presentation-layer x100 here.
    scaled = calculate_market_pulse_metrics(_history([100.0, 101.27]), AS_OF)
    assert scaled.daily_return == pytest.approx(0.0127)


def test_calculates_negative_returns_and_prefers_adjusted_close():
    closes = [200.0, 199.0]
    adjusted = [100.0, 90.0]
    metrics = calculate_market_pulse_metrics(
        _history(closes, adjusted=adjusted), AS_OF
    )

    assert metrics.close == 199.0
    assert metrics.daily_return == pytest.approx(-0.1)


def test_missing_adjusted_close_falls_back_to_consistent_close_series():
    metrics = calculate_market_pulse_metrics(
        _history([100.0, 102.0], adjusted=[50.0, None]), AS_OF
    )

    assert metrics.daily_return == pytest.approx(0.02)


def test_insufficient_history_is_independently_null():
    metrics = calculate_market_pulse_metrics(_history([100.0, 101.0]), AS_OF)

    assert metrics.daily_return == pytest.approx(0.01)
    assert metrics.weekly_return is None
    assert metrics.monthly_return is None
    assert metrics.pct_above_52w_low is None
    assert metrics.pct_below_52w_high is None


def test_duplicate_dates_are_coalesced_deterministically():
    history = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0]},
        index=pd.to_datetime(["2026-08-20", "2026-08-21", "2026-08-21"]),
    )

    metrics = calculate_market_pulse_metrics(history, AS_OF)

    assert metrics.close == 102.0
    assert metrics.daily_return == pytest.approx(0.02)


def test_missing_required_observation_is_not_skipped_or_replaced_with_zero():
    history = _history([100.0, 101.0, 102.0, None, 104.0, 105.0])

    metrics = calculate_market_pulse_metrics(history, AS_OF)

    assert metrics.daily_return == pytest.approx(105 / 104 - 1)
    assert metrics.weekly_return == pytest.approx(0.05)
    history.iloc[-2, 0] = float("nan")
    metrics = calculate_market_pulse_metrics(history, AS_OF)
    assert metrics.daily_return is None


def test_calculation_excludes_future_rows_without_lookahead():
    history = pd.DataFrame(
        {"Close": [100.0, 101.0, 1000.0]},
        index=pd.to_datetime(["2026-08-20", "2026-08-21", "2026-08-24"]),
    )

    metrics = calculate_market_pulse_metrics(history, AS_OF)

    assert metrics.close == 101.0
    assert metrics.daily_return == pytest.approx(0.01)


def test_daily_ranking_is_independent_stable_and_missing_last():
    ranked = rank_market_pulse_rows(
        [
            _row(MARKET_SEGMENTS, "ZZZ", 0.02),
            _row(MARKET_SEGMENTS, "AAA", 0.02),
            _row(MARKET_SEGMENTS, "MMM", None),
            _row(SECTORS, "SEC2", -0.01),
            _row(SECTORS, "SEC1", 0.01),
        ]
    )

    segment_rows = [row for row in ranked if row.section == MARKET_SEGMENTS]
    sector_rows = [row for row in ranked if row.section == SECTORS]
    assert [(row.ticker, row.rank) for row in segment_rows] == [
        ("AAA", 1),
        ("ZZZ", 2),
        ("MMM", 3),
    ]
    assert [(row.ticker, row.rank) for row in sector_rows] == [
        ("SEC1", 1),
        ("SEC2", 2),
    ]


def test_component_symbols_rank_by_63_session_strength_relative_to_spy():
    benchmark = _history([100.0 + index for index in range(80)])
    histories = {
        "WEAK": _history([100.0 + index * 0.5 for index in range(80)]),
        "STRONG": _history([100.0 + index * 2.0 for index in range(80)]),
    }

    strong_score = calculate_relative_strength_vs_benchmark(
        histories["STRONG"], benchmark, AS_OF, sessions=63
    )
    weak_score = calculate_relative_strength_vs_benchmark(
        histories["WEAK"], benchmark, AS_OF, sessions=63
    )
    ranked = rank_symbols_by_relative_strength(
        ["WEAK", "MISSING", "STRONG"],
        histories,
        benchmark,
        AS_OF,
        sessions=63,
    )

    assert strong_score is not None and weak_score is not None
    assert strong_score > weak_score
    assert ranked == ("STRONG", "WEAK", "MISSING")


def test_sql_repository_seeds_same_ticker_in_sections_and_upserts_idempotently():
    engine = create_engine("sqlite:///:memory:", future=True)
    repository = MarketPulseSnapshotRepository(engine)
    instruments = (
        MarketPulseInstrument(SECTORS, "Gold Miners", "GDX", 1),
        MarketPulseInstrument(INDUSTRIES_THEMES, "Gold Miners", "GDX", 1),
    )
    first = _snapshot(
        [_row(SECTORS, "GDX", 0.01), _row(INDUSTRIES_THEMES, "GDX", 0.01)]
    )

    assert repository.upsert_snapshot(first, instruments) == 2
    assert repository.upsert_snapshot(first, instruments) == 2
    inspector = inspect(engine)
    assert inspector.has_table("market_pulse_instruments")
    assert inspector.has_table("market_pulse_snapshots")

    metadata = __import__("sqlalchemy").MetaData()
    instrument_table = _get_market_pulse_instruments_table(metadata)
    snapshot_table = _get_market_pulse_snapshots_table(metadata)
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(instrument_table)) == 2
        assert conn.scalar(select(func.count()).select_from(snapshot_table)) == 2

    updated = _snapshot(
        [
            _row(SECTORS, "GDX", 0.05, stock1="AEM", stock2="NEM"),
            _row(
                INDUSTRIES_THEMES,
                "GDX",
                0.05,
                stock1="AEM",
                stock2="NEM",
            ),
        ]
    )
    repository.upsert_snapshot(updated, instruments)
    cached = repository.load_latest_snapshot()
    assert cached is not None
    assert len(cached.rows) == 2
    assert all(row.daily_return == pytest.approx(0.05) for row in cached.rows)
    assert all((row.stock1, row.stock2) == ("AEM", "NEM") for row in cached.rows)

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                instrument_table.insert().values(
                    section=SECTORS,
                    display_name="Duplicate",
                    ticker="GDX",
                    display_order=2,
                    is_active=True,
                    created_at=dt.datetime.now(),
                    updated_at=dt.datetime.now(),
                )
            )


class _FakeProvider:
    def __init__(self, histories, failures=None, entered=None, release=None):
        self.histories = histories
        self.failures = failures or {}
        self.entered = entered
        self.release = release
        self.calls = []

    def fetch(self, tickers, *, latest_completed_session):
        self.calls.append((list(tickers), latest_completed_session))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(5)
        return MarketPulseHistoryBatch(
            self.histories,
            self.failures,
            "fake_adjusted",
            pd.DataFrame(),
        )


def _service(tmp_path, provider, instruments):
    config_path = tmp_path / "market_pulse.json"
    _write_config(config_path, instruments)
    return MarketPulseService(
        provider=provider,
        config_path=config_path,
        cache_path=tmp_path / "snapshot.json",
    )


def test_successful_refresh_is_batched_ranked_and_cached(tmp_path):
    instruments = (
        MarketPulseInstrument(MARKET_SEGMENTS, "Alpha", "AAA", 1),
        MarketPulseInstrument(MARKET_SEGMENTS, "Beta", "BBB", 2),
    )
    provider = _FakeProvider(
        {"AAA": _history(range(100, 352)), "BBB": _history(range(200, 452))}
    )
    service = _service(tmp_path, provider, instruments)

    snapshot = service.refresh(
        now=dt.datetime(2026, 8, 22, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    )

    assert len(provider.calls) == 1
    assert provider.calls[0][0] == ["AAA", "BBB"]
    assert snapshot.as_of_date == AS_OF
    assert [row.rank for row in snapshot.rows] == [1, 2]
    assert service.load_cached_snapshot() is not None


def test_refresh_attaches_only_daily_universe_components(tmp_path, monkeypatch):
    class _ComponentsProvider(_FakeProvider):
        def fetch_components(self, tickers, *, eligible_symbols, limit):
            assert list(tickers) == ["GDX"]
            assert eligible_symbols == {
                "AEM": "Agnico Eagle Mines",
                "NEM": "Newmont",
                "WPM": "Wheaton Precious Metals",
                "AU": "Anglogold Ashanti",
            }
            assert limit == 10
            return MarketPulseComponentsBatch(
                {"GDX": ("AEM", "NEM", "WPM", "AU")},
                {},
                "fake_holdings",
            )

    monkeypatch.setattr(
        "src.services.market_pulse.get_default_universe_name_map",
        lambda **_kwargs: {
            "AEM": "Agnico Eagle Mines",
            "NEM": "Newmont",
            "WPM": "Wheaton Precious Metals",
            "AU": "Anglogold Ashanti",
        },
    )
    service = _service(
        tmp_path,
        _ComponentsProvider(
            {
                "GDX": _history(range(100, 352)),
                "SPY": _history([100.0 + index for index in range(252)]),
                "AEM": _history([100.0 + index for index in range(252)]),
                "NEM": _history([100.0 + index * 2 for index in range(252)]),
                "WPM": _history([100.0 + index * 3 for index in range(252)]),
                "AU": _history([100.0 + index * 4 for index in range(252)]),
            }
        ),
        (MarketPulseInstrument(SECTORS, "Gold Miners", "GDX", 1),),
    )

    snapshot = service.refresh(
        now=dt.datetime(2026, 8, 22, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    )

    row = snapshot.rows[0]
    assert (row.stock1, row.stock2, row.stock3, row.stock4) == (
        "AU",
        "WPM",
        "NEM",
        "AEM",
    )
    cached = service.load_cached_snapshot()
    assert cached is not None
    assert cached.rows[0].stock4 == "AEM"


def test_partial_failure_keeps_successes_and_marks_failed_row_unavailable(tmp_path):
    instruments = (
        MarketPulseInstrument(MARKET_SEGMENTS, "Alpha", "AAA", 1),
        MarketPulseInstrument(MARKET_SEGMENTS, "Missing", "BAD", 2),
    )
    service = _service(
        tmp_path,
        _FakeProvider(
            {"AAA": _history(range(100, 352))},
            {"BAD": "delisted or unavailable"},
        ),
        instruments,
    )

    snapshot = service.refresh(
        now=dt.datetime(2026, 8, 22, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    )

    rows = {row.ticker: row for row in snapshot.rows}
    assert rows["AAA"].status == "available"
    assert rows["BAD"].status == "unavailable"
    assert rows["BAD"].daily_return is None
    assert snapshot.failures["BAD"] == "delisted or unavailable"


def test_total_provider_failure_retains_previous_local_snapshot(tmp_path):
    instrument = MarketPulseInstrument(MARKET_SEGMENTS, "Alpha", "AAA", 1)
    service = _service(
        tmp_path,
        _FakeProvider({}, {"AAA": "network offline"}),
        (instrument,),
    )
    prior = _snapshot([_row(MARKET_SEGMENTS, "AAA", 0.01)])
    service.cache_path.write_text(json.dumps(snapshot_to_dict(prior)), encoding="utf-8")

    with pytest.raises(MarketPulseRefreshError, match="network offline"):
        service.refresh()

    retained = service.load_cached_snapshot()
    assert retained is not None
    assert retained.as_of_date == prior.as_of_date
    assert retained.rows[0].daily_return == prior.rows[0].daily_return


def test_cached_snapshot_respects_current_inactive_configuration(tmp_path):
    instruments = (
        MarketPulseInstrument(MARKET_SEGMENTS, "Active", "AAA", 1, True),
        MarketPulseInstrument(MARKET_SEGMENTS, "Inactive", "OLD", 2, False),
    )
    service = _service(tmp_path, _FakeProvider({}), instruments)
    prior = _snapshot(
        [
            _row(MARKET_SEGMENTS, "AAA", 0.01),
            _row(MARKET_SEGMENTS, "OLD", 0.02),
        ]
    )
    service.cache_path.write_text(json.dumps(snapshot_to_dict(prior)), encoding="utf-8")

    cached = service.load_cached_snapshot()

    assert cached is not None
    assert [row.ticker for row in cached.rows] == ["AAA"]


def test_concurrent_refresh_is_suppressed(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    provider = _FakeProvider(
        {"AAA": _history(range(100, 352))}, entered=entered, release=release
    )
    service = _service(
        tmp_path,
        provider,
        (MarketPulseInstrument(MARKET_SEGMENTS, "Alpha", "AAA", 1),),
    )
    failure = []

    def run_first():
        try:
            service.refresh()
        except Exception as exc:  # pragma: no cover - diagnostic guard
            failure.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(5)
    with pytest.raises(MarketPulseRefreshInProgress):
        service.refresh()
    release.set()
    thread.join(5)
    assert not failure
    assert len(provider.calls) == 1


def test_common_market_date_uses_majority_and_marks_lagging_ticker(tmp_path):
    prior = AS_OF - dt.timedelta(days=1)
    instruments = tuple(
        MarketPulseInstrument(MARKET_SEGMENTS, ticker, ticker, index)
        for index, ticker in enumerate(("AAA", "BBB", "OLD"), 1)
    )
    provider = _FakeProvider(
        {
            "AAA": _history(range(100, 352)),
            "BBB": _history(range(200, 452)),
            "OLD": _history(range(300, 552), end=prior),
        }
    )
    service = _service(tmp_path, provider, instruments)

    snapshot = service.refresh(
        now=dt.datetime(2026, 8, 22, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    )

    assert snapshot.as_of_date == AS_OF
    assert next(row for row in snapshot.rows if row.ticker == "OLD").status == "stale"


def test_refresh_merges_utc_cached_daily_bars_with_naive_provider_bars(
    tmp_path, monkeypatch
):
    instrument = MarketPulseInstrument(MARKET_SEGMENTS, "Alpha", "AAA", 1)
    cached = _history(range(100, 351), end=AS_OF - dt.timedelta(days=1))
    cached.index = cached.index.tz_localize("UTC")
    provider = _FakeProvider({"AAA": _history([350.0, 351.0])})
    service = _service(tmp_path, provider, (instrument,))
    service.set_engine(create_engine("sqlite:///:memory:"))
    monkeypatch.setattr(
        "src.services.market_pulse.load_universe_history_from_db",
        lambda *_args, **_kwargs: {"AAA": cached},
    )

    snapshot = service.refresh(
        now=dt.datetime(2026, 8, 22, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    )

    assert snapshot.as_of_date == AS_OF
    assert snapshot.rows[0].status == "available"
    assert snapshot.rows[0].close == pytest.approx(351.0)


def test_yfinance_provider_uses_one_batch_loader_call(monkeypatch):
    calls = []
    dates = pd.bdate_range(end=AS_OF, periods=2)
    columns = pd.MultiIndex.from_product(
        [["AAA", "BBB"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    raw = pd.DataFrame(
        [
            [99.0, 101.0, 98.0, 100.0, 1000.0, 199.0, 201.0, 198.0, 200.0, 2000.0],
            [100.0, 102.0, 99.0, 101.0, 1100.0, 201.0, 203.0, 200.0, 202.0, 2100.0],
        ],
        index=dates,
        columns=columns,
    )

    def fake_download(tickers, **kwargs):
        calls.append((list(tickers), kwargs))
        return raw

    monkeypatch.setattr("src.services.market_pulse.download_price_history", fake_download)

    result = YFinanceMarketPulseProvider().fetch(
        ["AAA", "BBB"], latest_completed_session=AS_OF
    )

    assert len(calls) == 1
    assert calls[0][0] == ["AAA", "BBB"]
    assert calls[0][1]["fallback_to_single"] is False
    assert calls[0][1]["timeout_seconds"] == 15.0
    assert set(result.histories) == {"AAA", "BBB"}


def test_yfinance_components_keep_weight_order_and_resolve_primary_listings(
    monkeypatch,
):
    holdings = pd.DataFrame(
        {
            "Name": ["Newmont", "Agnico", "Barrick", "Wheaton", "Anglogold"],
            "Holding Percent": [0.11, 0.10, 0.08, 0.06, 0.05],
        },
        index=["NEM", "AEM.TO", "ABX.TO", "WPM.TO", "AU"],
    )

    class _FundsData:
        top_holdings = holdings

    class _Ticker:
        funds_data = _FundsData()

    monkeypatch.setattr("src.services.market_pulse.yf.Ticker", lambda _symbol: _Ticker())

    result = YFinanceMarketPulseProvider().fetch_components(
        ["GDX"],
        eligible_symbols={
            "AEM": "Agnico Eagle Mines Ltd",
            "NEM": "Newmont Corp",
            "WPM": "Wheaton Precious Metals Corp",
            "AU": "Anglogold Ashanti PLC",
            "ABX": "Abacus Global Management Inc",
        },
        limit=4,
    )

    assert result.components["GDX"] == ("NEM", "AEM", "WPM", "AU")
    assert not result.failures


def test_provider_discards_partial_session_after_completed_market_date(monkeypatch):
    dates = pd.to_datetime(["2026-08-21", "2026-08-24"])
    columns = pd.MultiIndex.from_product(
        [["AAA"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    raw = pd.DataFrame(
        [
            [99.0, 101.0, 98.0, 100.0, 1000.0],
            [109.0, 111.0, 108.0, 110.0, 1200.0],
        ],
        index=dates,
        columns=columns,
    )
    monkeypatch.setattr(
        "src.services.market_pulse.download_price_history",
        lambda *_args, **_kwargs: raw,
    )

    result = YFinanceMarketPulseProvider().fetch(
        ["AAA"], latest_completed_session=AS_OF
    )

    assert result.raw_history.index.tolist() == [pd.Timestamp(AS_OF)]
    assert result.histories["AAA"].index.tolist() == [pd.Timestamp(AS_OF)]
