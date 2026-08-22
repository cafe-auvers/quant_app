from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, select

from src.core.chart_fundamentals import (
    ChartFundamentalContext,
    EarningsEvent,
    EventStatus,
    GrowthStatus,
    ProfileStatus,
    ReportTiming,
    StockProfile,
    StockProfileProviderResult,
    build_ttm_earnings_line,
    calculate_eps_growth,
    enrich_earnings_history,
    format_compact_growth_pair,
    next_upcoming_earnings,
)
from src.infrastructure.database.mirror_copy import sync_local_mirror_from_pc
from src.infrastructure.database.mirror_engine import (
    MIRRORED_TABLES,
    _BOOLEAN_RECONCILE_COLUMNS,
    _RECONCILE_TABLE_SPECS,
)
from src.infrastructure.database.mirror_reconciliation import (
    reconcile_local_mirror_with_pc,
)
from src.infrastructure.database.repositories.fundamentals import (
    EARNINGS_DATASET,
    PROFILE_DATASET,
    ensure_fundamental_tables,
    load_earnings_events,
    load_fundamental_sync_state,
    load_stock_profile,
    load_stock_profiles,
    record_fundamental_sync_state,
    seed_stock_profiles,
    upsert_earnings_events,
    upsert_earnings_events_bulk,
    upsert_stock_profile,
)
from src.services.chart_fundamentals import (
    ChartFundamentalService,
    get_universe_earnings_history_due_symbols,
    refresh_nasdaq_universe_stock_profiles,
    refresh_universe_earnings_history,
    refresh_universe_upcoming_earnings,
    refresh_universe_stock_profiles,
    seed_default_universe_stock_profiles,
)
from src.services.fundamental_providers import (
    NasdaqStockProfileUniverseProvider,
    NasdaqUpcomingEarningsUniverseProvider,
    YahooEarningsProvider,
    YahooStockProfileProvider,
)
from src.ui.charts.controller_data_flow import ChartsDataFlowMixin
from src.ui.charts.render_lightweight import ChartLightweightRenderMixin
from src.ui.fundamental_worker import ChartFundamentalRefreshWorker


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 21, 12, tzinfo=UTC)


class _JsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _event(
    fiscal_period: str,
    report_date: str,
    eps: float | None,
    *,
    symbol: str = "NVDA",
    diluted: float | None = None,
    basic: float | None = None,
    revenue: float | None = None,
) -> EarningsEvent:
    return EarningsEvent(
        symbol=symbol,
        event_key=f"FPE:{fiscal_period}",
        report_date=dt.date.fromisoformat(report_date),
        fiscal_period_end=dt.date.fromisoformat(fiscal_period),
        event_status=EventStatus.REPORTED,
        reported_eps=eps,
        statement_diluted_eps=diluted,
        statement_basic_eps=basic,
        revenue=revenue,
        source="yahoo",
        source_updated_at=NOW,
    )


def _six_quarters(eps_values=(1.0, 1.0, 1.0, 1.0, 2.24, 3.4944)):
    periods = (
        ("2025-03-31", "2025-05-05"),
        ("2025-06-30", "2025-08-05"),
        ("2025-09-30", "2025-11-05"),
        ("2025-12-31", "2026-02-05"),
        ("2026-03-31", "2026-05-05"),
        ("2026-06-30", "2026-08-05"),
    )
    return tuple(
        _event(fiscal, report, eps, revenue=100 + index * 20)
        for index, ((fiscal, report), eps) in enumerate(zip(periods, eps_values))
    )


@pytest.mark.parametrize(
    ("current", "prior", "value", "status", "token"),
    [
        (2.24, 1.0, 124.0, GrowthStatus.NORMAL, "124"),
        (0.88, 1.0, -12.0, GrowthStatus.NORMAL, "-12"),
        (1.0, 0.0, None, GrowthStatus.ZERO_BASE, "N/A"),
        (1.0, -1.0, None, GrowthStatus.TURNAROUND, "TURN"),
        (-1.0, 1.0, None, GrowthStatus.LOSS, "LOSS"),
        (-0.5, -1.0, None, GrowthStatus.NEGATIVE_NOT_MEANINGFUL, "N/M"),
        (None, 1.0, None, GrowthStatus.MISSING, "N/A"),
    ],
)
def test_eps_growth_edge_cases(current, prior, value, status, token):
    result = calculate_eps_growth(current, prior)
    assert result.value == pytest.approx(value) if value is not None else result.value is None
    assert result.status is status
    assert result.display_token == token


def test_growth_pair_uses_raw_values_and_previous_quarter_growth():
    enriched = enrich_earnings_history(_six_quarters())

    assert enriched[-1].eps_yoy_growth_pct == pytest.approx(249.44)
    assert enriched[-1].previous_eps_yoy_growth_pct == pytest.approx(124.0)
    assert format_compact_growth_pair(enriched[-1]) == "249/124"


def test_one_consistent_eps_basis_is_selected_for_every_comparison():
    rows = [
        replace(event, reported_eps=None, statement_diluted_eps=float(index + 1))
        for index, event in enumerate(_six_quarters())
    ]
    # A lone reported-EPS value cannot be mixed into the diluted sequence.
    rows[-1] = replace(rows[-1], reported_eps=99.0)

    enriched = enrich_earnings_history(rows)

    assert {event.eps_basis for event in enriched} == {"DILUTED"}
    assert enriched[-1].eps_yoy_growth_pct == pytest.approx(200.0)


def test_ttm_line_changes_only_on_report_date_and_forward_fills():
    enriched = enrich_earnings_history(_six_quarters((1, 1, 1, 1, 2, 3)))
    dates = pd.date_range("2026-07-01", "2026-08-07", freq="D")

    line = {point.date: point.value for point in build_ttm_earnings_line(enriched, dates)}

    assert line[dt.date(2026, 7, 1)] == pytest.approx(5.0)
    assert line[dt.date(2026, 7, 31)] == pytest.approx(5.0)
    assert line[dt.date(2026, 8, 4)] == pytest.approx(5.0)
    assert line[dt.date(2026, 8, 5)] == pytest.approx(7.0)
    assert line[dt.date(2026, 8, 7)] == pytest.approx(7.0)


def test_ttm_line_rejects_mixed_persisted_bases():
    rows = list(enrich_earnings_history(_six_quarters()))
    rows[-1] = replace(rows[-1], eps_basis="DILUTED")
    with pytest.raises(ValueError, match="inconsistent EPS bases"):
        build_ttm_earnings_line(rows, [dt.date(2026, 8, 5)])


@pytest.mark.parametrize(
    ("days", "flagged"),
    [(0, True), (1, True), (3, True), (14, True), (15, False), (-1, False)],
)
def test_upcoming_earnings_horizon_is_inclusive(days, flagged):
    as_of = dt.date(2026, 8, 21)
    event = EarningsEvent(
        symbol="NVDA",
        event_key="NEXT_EXPECTED",
        report_date=as_of + dt.timedelta(days=days),
        event_status=EventStatus.EXPECTED,
        source="yahoo",
        source_updated_at=NOW,
        is_date_estimated=True,
    )
    upcoming = next_upcoming_earnings([event], as_of=as_of)
    if days < 0:
        assert upcoming is None
    else:
        assert upcoming is not None
        assert upcoming.has_earnings_within_14d is flagged


def test_upcoming_earnings_uses_new_york_date_near_korea_midnight_boundary():
    # 2026-08-22 in Seoul is still 2026-08-21 in New York.
    now_kst = dt.datetime(2026, 8, 22, 8, tzinfo=ZoneInfo("Asia/Seoul"))
    event = EarningsEvent(
        symbol="NVDA",
        event_key="NEXT_EXPECTED",
        report_date=dt.date(2026, 8, 22),
        event_status=EventStatus.EXPECTED,
        source="yahoo",
        source_updated_at=NOW,
    )
    upcoming = next_upcoming_earnings([event], as_of=now_kst)
    assert upcoming is not None
    assert upcoming.days_to_earnings == 1


class _Ticker:
    def __init__(self, info=None, dates=None, statements=None, calendar=None):
        self._info = info
        self._dates = dates
        self._statements = statements
        self.calendar = calendar

    def get_info(self):
        return self._info

    def get_earnings_dates(self, limit=12):
        return self._dates

    def get_income_stmt(self, freq="yearly"):
        assert freq == "quarterly"
        return self._statements


class _Client:
    def __init__(self, ticker):
        self.value = ticker
        self.requested = []

    def ticker(self, symbol):
        self.requested.append(symbol)
        return self.value


@pytest.mark.parametrize(
    ("info", "company", "status"),
    [
        (
            {"longName": "NVIDIA", "shortName": "NVDA", "sector": "Technology", "industry": "Semiconductors"},
            "NVIDIA",
            ProfileStatus.OK,
        ),
        ({"shortName": "Short Name"}, "Short Name", ProfileStatus.PARTIAL),
        ({}, "NVDA", ProfileStatus.UNAVAILABLE),
        (
            {"longName": "SPDR S&P 500 ETF Trust", "quoteType": "ETF", "category": "Large Blend"},
            "SPDR S&P 500 ETF Trust",
            ProfileStatus.OK,
        ),
    ],
)
def test_yahoo_profile_normalization_and_fallbacks(info, company, status):
    client = _Client(_Ticker(info=info))
    provider = YahooStockProfileProvider(client, now=lambda: NOW)

    result = provider.fetch_stock_profile("NASDAQ:NVDA")

    assert result.symbol == "NVDA"
    assert result.profile.company_name == company
    assert result.status is status
    assert client.requested == ["NVDA"]


def test_yahoo_provider_keeps_canonical_class_share_and_maps_only_provider_symbol():
    client = _Client(_Ticker(info={"shortName": "Berkshire"}))
    result = YahooStockProfileProvider(client, now=lambda: NOW).fetch_stock_profile("BRK.B")
    assert result.profile.symbol == "BRK.B"
    assert result.profile.provider_symbol == "BRK-B"
    assert client.requested == ["BRK-B"]


def test_yahoo_earnings_normalizes_dates_statements_and_mutable_expected_event():
    date_index = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-08-27 16:00", tz="America/New_York"),
            pd.Timestamp("2026-05-05 16:00", tz="America/New_York"),
            pd.Timestamp("2026-02-05 08:00", tz="America/New_York"),
            pd.Timestamp("2025-11-05 16:00", tz="America/New_York"),
            pd.Timestamp("2025-08-05 16:00", tz="America/New_York"),
            pd.Timestamp("2025-05-05 16:00", tz="America/New_York"),
        ]
    )
    dates = pd.DataFrame(
        {
            "EPS Estimate": [1.1, 1, 1, 1, 1, 1],
            "Reported EPS": [None, 2, 1.5, 1.2, 1.0, 0.8],
            "Surprise(%)": [None, 0.1, 0.2, 0.1, 0, -0.1],
        },
        index=date_index,
    )
    columns = pd.to_datetime(
        ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"]
    )
    statements = pd.DataFrame(
        [[2, 1.5, 1.2, 1, 0.8], [2, 1.5, 1.2, 1, 0.8], [200, 150, 120, 100, 80]],
        index=["DilutedEPS", "BasicEPS", "TotalRevenue"],
        columns=columns,
    )
    client = _Client(_Ticker(dates=dates, statements=statements, calendar={}))

    result = YahooEarningsProvider(client, now=lambda: NOW).fetch_earnings("NVDA")

    assert len(result.events) == 6
    assert result.events[-1].event_key == "NEXT_EXPECTED"
    assert result.events[-1].report_date == dt.date(2026, 8, 27)
    assert result.events[-1].report_timing is ReportTiming.AMC
    assert result.events[-1].is_date_estimated is True
    reported = result.events[-2]
    assert reported.event_key == "FPE:2026-03-31"
    assert reported.revenue == 200
    assert reported.eps_basis == "REPORTED"


def _profile(symbol="NVDA", *, checked_at=NOW, status=ProfileStatus.OK):
    return StockProfile(
        symbol=symbol,
        provider_symbol=symbol.replace(".", "-"),
        company_name="NVIDIA Corporation" if symbol == "NVDA" else symbol,
        sector_name="Technology",
        sector_key="technology",
        industry_name="Semiconductors",
        industry_key="semiconductors",
        profile_status=status,
        source="yahoo",
        last_checked_at=checked_at,
        last_successful_sync_at=checked_at if status is not ProfileStatus.UNAVAILABLE else None,
        updated_at=checked_at,
    )


def test_kis_universe_seed_populates_every_symbol_and_is_idempotent(tmp_path):
    cache = tmp_path / "us_kis_tickers.csv"
    cache.write_text(
        "Symbol,KisSymbol,Exchange,Name,KoreanName,Currency\n"
        "AAPL,AAPL,NASD,Apple Inc.,,USD\n"
        "MSFT,MSFT,NASD,Microsoft Corporation,,USD\n",
        encoding="utf-8",
    )
    engine = create_engine("sqlite:///:memory:", future=True)

    assert seed_default_universe_stock_profiles(
        engine,
        universe=("AAPL", "MSFT"),
        cache_path=cache,
        now=NOW,
    ) == 2
    assert seed_default_universe_stock_profiles(
        engine,
        universe=("AAPL", "MSFT"),
        cache_path=cache,
        now=NOW + dt.timedelta(hours=1),
    ) == 0

    apple = load_stock_profile(engine, "AAPL")
    microsoft = load_stock_profile(engine, "MSFT")
    assert apple.company_name == "Apple Inc."
    assert microsoft.company_name == "Microsoft Corporation"
    assert apple.exchange == "NASD"
    assert apple.currency == "USD"
    assert apple.source == "kis_master"
    assert apple.profile_status is ProfileStatus.PARTIAL


def test_kis_seed_repairs_unavailable_placeholder_but_preserves_enrichment():
    engine = create_engine("sqlite:///:memory:", future=True)
    unavailable = replace(
        _profile("AAPL", status=ProfileStatus.UNAVAILABLE),
        company_name="AAPL",
        sector_name=None,
        sector_key=None,
        industry_name=None,
        industry_key=None,
        source="yahoo",
        last_successful_sync_at=None,
    )
    enriched = _profile("MSFT")
    upsert_stock_profile(engine, unavailable)
    upsert_stock_profile(engine, enriched)
    seeds = (
        replace(
            unavailable,
            company_name="Apple Inc.",
            exchange="NASD",
            currency="USD",
            profile_status=ProfileStatus.PARTIAL,
            source="kis_master",
            last_successful_sync_at=NOW,
        ),
        replace(
            enriched,
            company_name="Microsoft from KIS",
            sector_name=None,
            industry_name=None,
            profile_status=ProfileStatus.PARTIAL,
            source="kis_master",
        ),
    )

    assert seed_stock_profiles(engine, seeds) == 1
    assert load_stock_profile(engine, "AAPL").company_name == "Apple Inc."
    assert load_stock_profile(engine, "AAPL").source == "kis_master"
    assert load_stock_profile(engine, "MSFT").company_name == enriched.company_name
    assert load_stock_profile(engine, "MSFT").source == "yahoo"


def test_universe_profile_enrichment_is_bounded_and_advances_to_next_symbols():
    class ProfileProvider:
        def __init__(self):
            self.calls = []

        def fetch_stock_profile(self, symbol):
            self.calls.append(symbol)
            profile = _profile(symbol)
            return StockProfileProviderResult(
                symbol=symbol,
                profile=profile,
                status=ProfileStatus.OK,
            )

    engine = create_engine("sqlite:///:memory:", future=True)
    provider = ProfileProvider()
    service = ChartFundamentalService(
        engine,
        profile_provider=provider,
        earnings_provider=object(),
        now=lambda: NOW,
    )

    first = refresh_universe_stock_profiles(
        engine,
        ("AAPL", "MSFT", "NVDA"),
        max_symbols=2,
        as_of=NOW,
        service=service,
    )
    second = refresh_universe_stock_profiles(
        engine,
        ("AAPL", "MSFT", "NVDA"),
        max_symbols=2,
        as_of=NOW,
        service=service,
    )

    assert first == {
        "universe": 3,
        "due": 3,
        "attempted": 2,
        "refreshed": 2,
        "unavailable": 0,
        "remaining": 1,
    }
    assert second["attempted"] == 1
    assert second["remaining"] == 0
    assert provider.calls == ["AAPL", "MSFT", "NVDA"]


def test_nasdaq_universe_provider_normalizes_share_classes_and_profile_fields():
    payload = {
        "data": {
            "rows": [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc. Common Stock",
                    "country": "United States",
                    "sector": "Technology",
                    "industry": "Consumer Electronics",
                },
                {
                    "symbol": "BRK/B",
                    "name": "Berkshire Hathaway Inc.",
                    "country": "United States",
                    "sector": "Consumer Discretionary",
                    "industry": "Diversified Holdings",
                },
            ]
        }
    }
    provider = NasdaqStockProfileUniverseProvider(
        http_get=lambda *_args, **_kwargs: _JsonResponse(payload),
        now=lambda: NOW,
    )

    profiles = provider.fetch_stock_profiles(("AAPL", "BRK-B", "MSFT"))

    assert [profile.symbol for profile in profiles] == ["AAPL", "BRK-B"]
    assert profiles[0].sector_name == "Technology"
    assert profiles[0].industry_name == "Consumer Electronics"
    assert profiles[0].profile_status is ProfileStatus.OK


def test_nasdaq_profile_refresh_preserves_baseline_exchange_and_is_idempotent():
    engine = create_engine("sqlite:///:memory:", future=True)
    baseline = replace(
        _profile("AAPL"),
        company_name="Apple baseline",
        exchange="NASD",
        sector_name=None,
        industry_name=None,
        profile_status=ProfileStatus.PARTIAL,
        source="kis_master",
    )
    seed_stock_profiles(engine, (baseline,))
    provider = SimpleNamespace(
        fetch_stock_profiles=lambda _symbols: (
            replace(
                baseline,
                company_name="Apple Inc.",
                exchange=None,
                sector_name="Technology",
                industry_name="Consumer Electronics",
                profile_status=ProfileStatus.OK,
                source="nasdaq",
            ),
        )
    )

    first = refresh_nasdaq_universe_stock_profiles(
        engine, ("AAPL",), provider=provider
    )
    second = refresh_nasdaq_universe_stock_profiles(
        engine, ("AAPL",), provider=provider
    )
    stored = load_stock_profiles(engine)["AAPL"]

    assert first["complete"] == 1
    assert first["changed"] == 1
    assert second["changed"] == 0
    assert stored.exchange == "NASD"
    assert stored.sector_name == "Technology"
    assert stored.industry_name == "Consumer Electronics"


def test_nasdaq_earnings_calendar_creates_expected_and_prior_year_events():
    def get(_url, *, params, **_kwargs):
        rows = []
        if params["date"] == "2026-08-24":
            rows = [{
                "symbol": "AAPL",
                "time": "time-pre-market",
                "fiscalQuarterEnding": "Jun/2026",
                "epsForecast": "$1.25",
                "lastYearRptDt": "8/25/2025",
                "lastYearEPS": "($0.50)",
            }]
        return _JsonResponse({"data": {"rows": rows}})

    provider = NasdaqUpcomingEarningsUniverseProvider(
        http_get=get,
        now=lambda: NOW,
    )

    events = provider.fetch_earnings(("AAPL", "MSFT"), horizon_days=3)
    expected = next(event for event in events if event.event_status is EventStatus.EXPECTED)
    prior = next(event for event in events if event.event_status is EventStatus.REPORTED)

    assert expected.symbol == "AAPL"
    assert expected.report_date == dt.date(2026, 8, 24)
    assert expected.report_timing is ReportTiming.BMO
    assert expected.estimated_eps == pytest.approx(1.25)
    assert expected.fiscal_period_end == dt.date(2026, 6, 30)
    assert prior.reported_eps == pytest.approx(-0.5)
    assert prior.eps_basis == "REPORTED"


def test_nasdaq_earnings_calendar_keeps_other_dates_after_retried_failure():
    def get(_url, *, params, **_kwargs):
        if params["date"] == "2026-08-21":
            raise OSError("temporary socket failure")
        rows = [{"symbol": "AAPL"}] if params["date"] == "2026-08-24" else []
        return _JsonResponse({"data": {"rows": rows}})

    provider = NasdaqUpcomingEarningsUniverseProvider(
        http_get=get,
        now=lambda: NOW,
    )

    events = provider.fetch_earnings(("AAPL",), horizon_days=3)

    assert any(event.symbol == "AAPL" for event in events)
    assert provider.failed_dates == (dt.date(2026, 8, 21),)


def test_bulk_earnings_calendar_persistence_is_idempotent():
    engine = create_engine("sqlite:///:memory:", future=True)
    event = EarningsEvent(
        symbol="AAPL",
        event_key="NEXT_EXPECTED",
        report_date=dt.date(2026, 10, 29),
        event_status=EventStatus.EXPECTED,
        source="nasdaq",
        source_updated_at=NOW,
    )
    provider = SimpleNamespace(
        fetch_earnings=lambda _symbols, horizon_days: (event,)
    )

    first = refresh_universe_upcoming_earnings(
        engine, ("AAPL",), provider=provider
    )
    second = refresh_universe_upcoming_earnings(
        engine, ("AAPL",), provider=provider
    )

    assert first == {
        "universe": 1,
        "events": 1,
        "symbols": 1,
        "changed": 1,
        "failed_dates": 0,
    }
    assert second["changed"] == 0
    stored = load_earnings_events(engine, "AAPL")
    assert len(stored) == 1
    assert stored[0].event_key == "NEXT_EXPECTED"
    assert stored[0].report_date == dt.date(2026, 10, 29)


def test_earnings_history_batch_prioritizes_never_attempted_symbols():
    engine = create_engine("sqlite:///:memory:", future=True)
    ensure_fundamental_tables(engine)
    record_fundamental_sync_state(
        engine,
        symbol="AAPL",
        dataset=EARNINGS_DATASET,
        status=ProfileStatus.OK,
        source="yahoo",
        checked_at=NOW - dt.timedelta(days=2),
        successful_at=NOW - dt.timedelta(days=2),
        payload_fingerprint="old",
    )

    class EarningsService:
        def __init__(self):
            self.calls = []

        def refresh_earnings(self, symbol):
            self.calls.append(symbol)
            return ProfileStatus.OK

    service = EarningsService()
    assert get_universe_earnings_history_due_symbols(
        engine,
        ("AAPL", "MSFT", "NVDA"),
        as_of=NOW,
    ) == ("MSFT", "NVDA", "AAPL")
    summary = refresh_universe_earnings_history(
        engine,
        ("AAPL", "MSFT", "NVDA"),
        max_symbols=2,
        as_of=NOW,
        service=service,
    )

    assert service.calls == ["MSFT", "NVDA"]
    assert summary["attempted"] == 2


def test_fundamental_tables_indexes_and_idempotent_accumulating_upserts():
    engine = create_engine("sqlite:///:memory:", future=True)
    ensure_fundamental_tables(engine)
    inspector = inspect(engine)

    assert {"stock_profiles", "earnings_events"}.issubset(inspector.get_table_names())
    assert "ix_stock_profiles_sector_key" in {item["name"] for item in inspector.get_indexes("stock_profiles")}
    assert "ix_stock_profiles_industry_key" in {item["name"] for item in inspector.get_indexes("stock_profiles")}
    assert "ix_earnings_events_symbol_report_date" in {item["name"] for item in inspector.get_indexes("earnings_events")}

    assert upsert_stock_profile(engine, _profile()) is True
    original_revision = load_stock_profile(engine, "NVDA").updated_at
    assert upsert_stock_profile(engine, replace(_profile(), last_checked_at=NOW + dt.timedelta(hours=1))) is False
    assert load_stock_profile(engine, "NVDA").updated_at == original_revision

    revised_profile = replace(
        _profile(),
        sector_name="Communication Services",
        sector_key="communication-services",
        industry_name="Interactive Media",
        industry_key="interactive-media",
        updated_at=NOW + dt.timedelta(days=1),
    )
    assert upsert_stock_profile(engine, revised_profile) is True
    stored_profile = load_stock_profile(engine, "NVDA")
    assert stored_profile.sector_name == "Communication Services"
    assert stored_profile.industry_name == "Interactive Media"

    events = enrich_earnings_history(_six_quarters())
    assert upsert_earnings_events(engine, events) == 6
    assert upsert_earnings_events(engine, events) == 0
    # A shorter provider response updates its rows but never truncates history.
    assert upsert_earnings_events(engine, events[-2:]) == 0
    assert len(load_earnings_events(engine, "NVDA")) == 6


def test_expected_event_date_is_revised_in_place_without_touching_history():
    engine = create_engine("sqlite:///:memory:", future=True)
    ensure_fundamental_tables(engine)
    reported = enrich_earnings_history(_six_quarters())
    expected = EarningsEvent(
        symbol="NVDA",
        event_key="NEXT_EXPECTED",
        report_date=dt.date(2026, 8, 24),
        event_status=EventStatus.EXPECTED,
        source="yahoo",
        source_updated_at=NOW,
        is_date_estimated=True,
    )
    upsert_earnings_events(engine, (*reported, expected), replace_expected=True, symbol="NVDA")

    revised = replace(expected, report_date=dt.date(2026, 8, 26))
    upsert_earnings_events(engine, (revised,), replace_expected=True, symbol="NVDA")
    rows = load_earnings_events(engine, "NVDA")

    assert len(rows) == 7
    assert [item.report_date for item in rows if item.event_status is EventStatus.EXPECTED] == [dt.date(2026, 8, 26)]
    assert len([item for item in rows if item.event_status is EventStatus.REPORTED]) == 6


def test_context_joins_optional_profile_and_earnings_by_canonical_symbol_only():
    engine = create_engine("sqlite:///:memory:", future=True)
    ensure_fundamental_tables(engine)
    upsert_stock_profile(engine, _profile("NVDA"))
    upsert_stock_profile(engine, _profile("AAPL"))
    upsert_earnings_events(engine, enrich_earnings_history(_six_quarters()))

    context = ChartFundamentalService(engine).load_chart_fundamental_context(
        "NASDAQ:NVDA", chart_dates=pd.date_range("2026-08-01", periods=7)
    )

    assert context.symbol == "NVDA"
    assert context.stock_profile.symbol == "NVDA"
    assert {event.symbol for event in context.earnings_events} == {"NVDA"}
    assert context.earnings_line


def test_unavailable_sync_state_is_negative_cached_for_24_hours():
    engine = create_engine("sqlite:///:memory:", future=True)
    ensure_fundamental_tables(engine)
    record_fundamental_sync_state(
        engine,
        symbol="NVDA",
        dataset=PROFILE_DATASET,
        status=ProfileStatus.UNAVAILABLE,
        source="yahoo",
        checked_at=NOW,
        successful_at=None,
        payload_fingerprint="empty",
    )
    record_fundamental_sync_state(
        engine,
        symbol="NVDA",
        dataset=EARNINGS_DATASET,
        status=ProfileStatus.UNAVAILABLE,
        source="yahoo",
        checked_at=NOW,
        successful_at=None,
        payload_fingerprint="empty",
    )
    service = ChartFundamentalService(engine, now=lambda: NOW)
    assert service.refresh_required("NVDA", as_of=NOW + dt.timedelta(hours=23)) is False
    assert service.refresh_required("NVDA", as_of=NOW + dt.timedelta(hours=24)) is True


def test_provider_failure_preserves_cached_profile_and_earnings():
    class FailingProfileProvider:
        def fetch_stock_profile(self, _symbol):
            raise RuntimeError("profile offline")

    class FailingEarningsProvider:
        def fetch_earnings(self, _symbol):
            raise RuntimeError("earnings offline")

    engine = create_engine("sqlite:///:memory:", future=True)
    ensure_fundamental_tables(engine)
    upsert_stock_profile(engine, _profile())
    upsert_earnings_events(engine, enrich_earnings_history(_six_quarters()))
    service = ChartFundamentalService(
        engine,
        profile_provider=FailingProfileProvider(),
        earnings_provider=FailingEarningsProvider(),
        now=lambda: NOW,
    )

    context = service.refresh_symbol("NVDA")

    assert context.stock_profile.company_name == "NVIDIA Corporation"
    assert len(context.earnings_events) == 6
    assert load_fundamental_sync_state(engine, "NVDA", PROFILE_DATASET)["status"] == "UNAVAILABLE"
    assert load_fundamental_sync_state(engine, "NVDA", EARNINGS_DATASET)["status"] == "UNAVAILABLE"


def test_new_tables_participate_in_one_way_local_mirror_and_boolean_normalization():
    names = {name for name, _watermark in MIRRORED_TABLES}
    specs = {spec.table_name: spec for spec in _RECONCILE_TABLE_SPECS}
    assert {"stock_profiles", "earnings_events"}.issubset(names)
    assert specs["stock_profiles"].primary_key == ("symbol",)
    assert specs["earnings_events"].primary_key == ("symbol", "event_key")
    assert "is_date_estimated" in _BOOLEAN_RECONCILE_COLUMNS

    pc = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    ensure_fundamental_tables(pc)
    ensure_fundamental_tables(local)
    upsert_stock_profile(pc, _profile())
    expected = EarningsEvent(
        symbol="NVDA",
        event_key="NEXT_EXPECTED",
        report_date=dt.date(2026, 8, 27),
        event_status=EventStatus.EXPECTED,
        source="yahoo",
        source_updated_at=NOW,
        is_date_estimated=True,
    )
    upsert_earnings_events(pc, [expected])

    written = sync_local_mirror_from_pc(
        pc,
        local,
        tables=(("stock_profiles", "updated_at"), ("earnings_events", "updated_at")),
    )

    assert written == {"stock_profiles": 1, "earnings_events": 1}
    assert load_stock_profile(local, "NVDA").company_name == "NVIDIA Corporation"
    assert load_earnings_events(local, "NVDA")[0].is_date_estimated is True
    reconciliation = reconcile_local_mirror_with_pc(
        pc,
        local,
        tables=(("stock_profiles", "updated_at"), ("earnings_events", "updated_at")),
        verify_derived=False,
    )
    assert reconciliation.success is True


def _history():
    return pd.DataFrame(
        {
            "Open": [10, 11, 12, 13, 14],
            "High": [11, 12, 13, 14, 15],
            "Low": [9, 10, 11, 12, 13],
            "Close": [10.5, 11.5, 12.5, 13.5, 14.5],
            "Volume": [100] * 5,
        },
        index=pd.date_range("2026-08-17", periods=5, freq="B"),
    )


def test_chart_html_renders_escaped_watermark_markers_badge_and_independent_scale():
    event = replace(
        enrich_earnings_history(_six_quarters())[-1],
        report_date=dt.date(2026, 8, 19),
    )
    expected = EarningsEvent(
        symbol="NVDA",
        event_key="NEXT_EXPECTED",
        report_date=dt.date(2026, 8, 27),
        event_status=EventStatus.EXPECTED,
        source="yahoo",
        source_updated_at=NOW,
        report_timing=ReportTiming.AMC,
        is_date_estimated=True,
    )
    upcoming = next_upcoming_earnings([expected], as_of=dt.date(2026, 8, 21))
    profile = replace(_profile(), company_name="NVIDIA <script>alert(1)</script>")
    line = build_ttm_earnings_line(
        enrich_earnings_history(_six_quarters()), _history().index
    )

    page = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "NASDAQ:NVDA",
        _history(),
        storage_symbol="NVDA",
        stock_profile=profile,
        earnings_events=[event],
        earnings_line=line,
        upcoming_earnings=upcoming,
    )

    assert page.index("watermark-symbol") < page.index("watermark-company")
    assert "NVIDIA &lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "NVIDIA <script>alert(1)</script>" not in page
    assert "Technology Sector" in page
    assert "Semiconductors" in page
    assert "pointer-events: none" in page
    assert "background: rgba(107, 114, 128, 0.30)" in page
    assert "E 6d AMC" in page
    assert "249/124" in page
    assert "priceScaleId: 'earnings'" in page
    assert "chart.priceScale('earnings')" in page
    assert "candleSeries.setMarkers" not in page
    assert 'id="earnings-event-layer"' in page
    assert "className = 'earnings-event-badge'" in page
    assert '<path d="M7 1 H23 L29 7 V23 H19 L15 31 L11 23 H1 V7 Z">' in page
    assert "chart.timeScale().timeToCoordinate(marker.time)" in page
    assert "bottom: 24px" in page
    assert "carriedEarningsLines(activeCrosshairTime)" in page
    assert "restoreChartTooltip();" in page
    assert '"time": "2026-08-27"' in page
    assert "mergeSeriesData(futureWhitespace, earningsWhitespace, candles)" in page


def test_earnings_badges_use_surprise_colors_and_not_price_coordinates():
    base = replace(
        enrich_earnings_history(_six_quarters())[-1],
        report_date=dt.date(2026, 8, 19),
    )
    positive = replace(
        base,
        event_key="positive",
        report_date=dt.date(2026, 8, 18),
        reported_eps=1.20,
        estimated_eps=1.00,
    )
    negative = replace(
        base,
        event_key="negative",
        report_date=dt.date(2026, 8, 19),
        reported_eps=0.80,
        estimated_eps=1.00,
    )
    neutral = replace(
        base,
        event_key="neutral",
        report_date=dt.date(2026, 8, 20),
        reported_eps=1.00,
        estimated_eps=1.00,
    )

    page = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "NVDA",
        _history(),
        storage_symbol="NVDA",
        earnings_events=[positive, negative, neutral],
    )

    assert '"color": "#089981"' in page
    assert '"color": "#f23645"' in page
    assert '"color": "#787b86"' in page
    assert '"label": "E"' in page
    assert '"position": "aboveBar"' not in page
    assert '"shape": "circle"' not in page


def test_latest_reported_earnings_remains_available_before_visible_chart_range():
    earlier_report = replace(
        enrich_earnings_history(_six_quarters())[-1],
        report_date=dt.date(2026, 8, 1),
    )

    page = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "NVDA",
        _history(),
        storage_symbol="NVDA",
        earnings_events=[earlier_report],
    )

    assert "const earningsMarkers = [];" in page
    assert "Report date: 2026-08-01" in page
    assert '"reported": true' in page
    assert ".filter(item => item.reported)" in page
    assert "time == null" in page
    assert "reportedEarningsSortValues" in page


def test_earnings_badges_reuse_dom_nodes_during_pan_and_resize():
    page = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "NVDA",
        _history(),
        storage_symbol="NVDA",
        earnings_events=enrich_earnings_history(_six_quarters()),
    )

    assert "const earningsBadgeEntries" in page
    assert "scheduleEarningsEventBadgeRender" in page
    assert "window.requestAnimationFrame" in page
    assert "earningsEventLayer.replaceChildren()" not in page


def test_chart_library_is_a_cacheable_local_asset():
    page = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "NVDA", _history()
    )

    assert 'src="file:///' in page
    assert 'data-lightweight-charts-version="4.2.3"' in page
    assert "unpkg.com" not in page


def test_renderer_omits_profile_fields_from_the_wrong_symbol():
    page = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "NVDA", _history(), storage_symbol="NVDA", stock_profile=_profile("AAPL")
    )
    assert "NVIDIA Corporation" not in page
    assert '<div class="watermark-symbol">NVDA</div>' in page


def test_stale_worker_completion_cannot_rerender_another_symbol():
    class Combo:
        def currentText(self):
            return "TSLA"

    window = SimpleNamespace(
        tradingview_symbol_combo=Combo(),
        _chart_fundamental_request_generation=3,
        tradingview_refresh_timestamps={},
        load_tradingview_chart=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale worker must not rerender")
        ),
    )
    context = ChartFundamentalContext(symbol="AAPL")
    ChartsDataFlowMixin._on_chart_fundamental_refresh_completed(window, context, 2)


def test_active_worker_completion_warms_cache_without_reloading_chart():
    engine = object()
    refresh_timestamps = {"single|NVDA|1D|fundamentals=old": NOW}
    window = SimpleNamespace(
        db_engine=engine,
        tradingview_refresh_timestamps=refresh_timestamps,
        load_tradingview_chart=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("earnings refresh must not reload the visible chart")
        ),
    )
    context = ChartFundamentalContext(
        symbol="NVDA",
        earnings_events=tuple(enrich_earnings_history(_six_quarters())),
        revision_token="new",
    )

    ChartsDataFlowMixin._on_chart_fundamental_refresh_completed(window, context, 4)

    cached = window._chart_fundamental_context_cache[(id(engine), "NVDA", 14)]
    assert cached[1] is context
    assert window.tradingview_refresh_timestamps is refresh_timestamps
    assert refresh_timestamps == {"single|NVDA|1D|fundamentals=old": NOW}


def test_chart_fundamental_context_reuses_short_lived_memory_cache(monkeypatch):
    calls = []

    class Service:
        def __init__(self, engine):
            self.engine = engine

        def load_chart_fundamental_context(self, symbol, **_kwargs):
            calls.append(symbol)
            return ChartFundamentalContext(symbol=symbol, revision_token=str(len(calls)))

    monkeypatch.setattr(
        "src.ui.charts.controller_data_flow.ChartFundamentalService", Service
    )
    window = SimpleNamespace(db_engine=object(), db_enabled=True)

    first = ChartsDataFlowMixin._load_cached_chart_fundamental_context(
        window, "NVDA", now=NOW, horizon_days=14
    )
    second = ChartsDataFlowMixin._load_cached_chart_fundamental_context(
        window,
        "NVDA",
        now=NOW + dt.timedelta(seconds=10),
        horizon_days=14,
    )
    refreshed = ChartsDataFlowMixin._load_cached_chart_fundamental_context(
        window,
        "NVDA",
        now=NOW + dt.timedelta(seconds=31),
        horizon_days=14,
    )

    assert first is second
    assert refreshed is not first
    assert calls == ["NVDA", "NVDA"]


def test_fundamental_worker_checks_freshness_off_the_ui_thread():
    refreshes = []
    skipped = []

    class Service:
        def __init__(self, _engine):
            pass

        def refresh_required(self, symbol):
            return False

        def refresh_symbol(self, symbol):
            refreshes.append(symbol)
            return ChartFundamentalContext(symbol=symbol)

    worker = ChartFundamentalRefreshWorker(
        object(), "NVDA", 4, service_factory=Service
    )
    worker.not_required.connect(
        lambda symbol, generation: skipped.append((symbol, generation))
    )

    worker.run()

    assert refreshes == []
    assert skipped == [("NVDA", 4)]


def test_split_view_loads_one_context_and_schedules_one_provider_refresh():
    class Combo:
        def __init__(self, text):
            self.text = text

        def currentText(self):
            return self.text

    class Check:
        def __init__(self, checked=True):
            self.checked = checked

        def isChecked(self):
            return self.checked

    class View:
        def setVisible(self, _visible):
            pass

    class Label:
        def setText(self, _text):
            pass

    class Window(ChartsDataFlowMixin):
        def __init__(self):
            self.tradingview_symbol_combo = Combo("NVDA")
            self.tradingview_split_screen_checkbox = Check(True)
            self.tradingview_split_chart_view = View()
            self.tradingview_chart_view = View()
            self.tradingview_status_label = Label()
            self.context_loads = 0
            self.refresh_schedules = 0
            self.rendered_contexts = []

        def _to_tradingview_symbol(self, symbol):
            return symbol

        def _get_tradingview_window_days(self):
            return 7

        def _load_cached_chart_fundamental_context(self, symbol, **_kwargs):
            self.context_loads += 1
            return ChartFundamentalContext(symbol=symbol, revision_token="one")

        def _render_tradingview_chart_view(self, _view, **kwargs):
            self.rendered_contexts.append(kwargs["fundamental_context"])
            return kwargs["timeframe"]

        def _schedule_chart_fundamental_refresh(self, *_args, **_kwargs):
            self.refresh_schedules += 1

    window = Window()

    window.load_tradingview_chart()

    assert window.context_loads == 1
    assert window.refresh_schedules == 1
    assert len(window.rendered_contexts) == 2
    assert window.rendered_contexts[0] is window.rendered_contexts[1]


def test_cached_earnings_are_passed_to_the_initial_chart_render():
    event = enrich_earnings_history(_six_quarters())[-1]
    context = ChartFundamentalContext(
        symbol="NVDA",
        earnings_events=(event,),
        revision_token="cached",
    )
    order = []

    class Combo:
        def currentText(self):
            return "NVDA"

    class View:
        def setVisible(self, _visible):
            pass

    class Label:
        def setText(self, _text):
            pass

    class Window(ChartsDataFlowMixin):
        tradingview_symbol_combo = Combo()
        tradingview_chart_view = View()
        tradingview_split_chart_view = View()
        tradingview_status_label = Label()

        def _to_tradingview_symbol(self, symbol):
            return symbol

        def _get_tradingview_window_days(self):
            return 7

        def _load_cached_chart_fundamental_context(self, *_args, **_kwargs):
            order.append("cached earnings")
            return context

        def _render_tradingview_chart_view(self, _view, **kwargs):
            order.append("initial render")
            assert kwargs["fundamental_context"].earnings_events == (event,)
            return "loaded"

        def _schedule_chart_fundamental_refresh(self, *_args, **_kwargs):
            order.append("background refresh")

    Window().load_tradingview_chart()

    assert order == ["cached earnings", "initial render", "background refresh"]
