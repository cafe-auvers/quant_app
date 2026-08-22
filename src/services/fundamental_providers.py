"""Replaceable Yahoo/yfinance adapters for chart supplemental data."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import replace
from typing import Callable, Optional, Protocol
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from src.core.chart_fundamentals import (
    EarningsEvent,
    EarningsProviderResult,
    EventStatus,
    ProfileStatus,
    ReportTiming,
    StockProfile,
    StockProfileProviderResult,
    canonical_symbol,
    clean_optional_text,
    enrich_earnings_history,
)
from src.utils.data_loader import normalize_yahoo_symbol


US_MARKET_ZONE = ZoneInfo("America/New_York")


class StockProfileProvider(Protocol):
    def fetch_stock_profile(self, symbol: str) -> StockProfileProviderResult:
        ...


class EarningsProvider(Protocol):
    def fetch_earnings(self, symbol: str) -> EarningsProviderResult:
        ...


class YahooTickerClient:
    """Share one lazy yfinance Ticker instance across provider adapters."""

    def __init__(self, ticker_factory: Callable[[str], object] = yf.Ticker):
        self._ticker_factory = ticker_factory
        self._tickers: dict[str, object] = {}

    def ticker(self, provider_symbol: str):
        if provider_symbol not in self._tickers:
            self._tickers[provider_symbol] = self._ticker_factory(provider_symbol)
        return self._tickers[provider_symbol]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _finite(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def yahoo_provider_symbol(symbol: str) -> str:
    """Map only provider spelling while preserving Yahoo exchange suffixes."""
    canonical = canonical_symbol(symbol)
    if canonical.endswith((".KS", ".KQ")):
        return canonical
    return normalize_yahoo_symbol(canonical)


class YahooStockProfileProvider:
    def __init__(
        self,
        client: Optional[YahooTickerClient] = None,
        *,
        now: Callable[[], dt.datetime] = _utcnow,
    ):
        self._client = client or YahooTickerClient()
        self._now = now

    def fetch_stock_profile(self, symbol: str) -> StockProfileProviderResult:
        canonical = canonical_symbol(symbol)
        provider_symbol = yahoo_provider_symbol(canonical)
        checked_at = self._now()
        errors = []
        try:
            raw = self._client.ticker(provider_symbol).get_info() or {}
            if not isinstance(raw, dict):
                raw = {}
        except Exception as exc:  # provider/network boundary
            raw = {}
            errors.append(str(exc))

        long_name = clean_optional_text(raw.get("longName"))
        short_name = clean_optional_text(raw.get("shortName"))
        company_name = long_name or short_name or canonical
        quote_type = clean_optional_text(raw.get("quoteType"))
        sector = clean_optional_text(raw.get("sector"))
        industry = clean_optional_text(raw.get("industry"))
        category = clean_optional_text(raw.get("category"))
        fund_family = clean_optional_text(raw.get("fundFamily"))
        meaningful = any(
            (
                long_name,
                short_name,
                quote_type,
                sector,
                industry,
                category,
                fund_family,
            )
        )
        is_fund = str(quote_type or "").upper() in {
            "ETF",
            "MUTUALFUND",
            "MUTUAL FUND",
            "FUND",
        }
        complete = bool(company_name and ((sector and industry) or (is_fund and category)))
        status = (
            ProfileStatus.OK
            if complete
            else ProfileStatus.PARTIAL
            if meaningful
            else ProfileStatus.UNAVAILABLE
        )
        successful_at = checked_at if status is not ProfileStatus.UNAVAILABLE else None
        profile = StockProfile(
            symbol=canonical,
            provider_symbol=provider_symbol,
            company_name=company_name,
            short_name=short_name,
            quote_type=quote_type,
            exchange=clean_optional_text(raw.get("exchange")),
            market=clean_optional_text(raw.get("market")),
            currency=clean_optional_text(raw.get("currency")),
            country=clean_optional_text(raw.get("country")),
            sector_name=sector,
            sector_key=clean_optional_text(raw.get("sectorKey")),
            industry_name=industry,
            industry_key=clean_optional_text(raw.get("industryKey")),
            category=category,
            fund_family=fund_family,
            profile_status=status,
            source="yahoo",
            last_checked_at=checked_at,
            last_successful_sync_at=successful_at,
            updated_at=checked_at,
        )
        return StockProfileProviderResult(
            symbol=canonical,
            profile=profile,
            status=status,
            errors=tuple(errors),
        )


class YahooEarningsProvider:
    def __init__(
        self,
        client: Optional[YahooTickerClient] = None,
        *,
        now: Callable[[], dt.datetime] = _utcnow,
        history_limit: int = 40,
    ):
        self._client = client or YahooTickerClient()
        self._now = now
        self._history_limit = max(12, min(100, int(history_limit)))

    def fetch_earnings(self, symbol: str) -> EarningsProviderResult:
        canonical = canonical_symbol(symbol)
        provider_symbol = yahoo_provider_symbol(canonical)
        checked_at = self._now()
        ticker = self._client.ticker(provider_symbol)
        errors = []

        dates = None
        try:
            dates = ticker.get_earnings_dates(limit=self._history_limit)
        except Exception as exc:  # provider/network boundary
            errors.append(f"earnings_dates: {exc}")

        statements = None
        try:
            statements = ticker.get_income_stmt(freq="quarterly")
        except Exception as exc:  # provider/network boundary
            errors.append(f"quarterly_income_stmt: {exc}")

        calendar = None
        try:
            calendar = ticker.calendar
        except Exception as exc:  # provider/network boundary
            errors.append(f"calendar: {exc}")

        events = self._normalize_events(
            canonical,
            dates,
            statements,
            calendar,
            checked_at=checked_at,
        )
        status = (
            ProfileStatus.OK
            if events and not errors
            else ProfileStatus.PARTIAL
            if events
            else ProfileStatus.UNAVAILABLE
        )
        return EarningsProviderResult(
            symbol=canonical,
            events=events,
            status=status,
            checked_at=checked_at,
            source="yahoo",
            errors=tuple(errors),
        )

    @staticmethod
    def _normalize_events(
        symbol: str,
        earnings_dates,
        statements,
        calendar,
        *,
        checked_at: dt.datetime,
    ) -> tuple[EarningsEvent, ...]:
        market_date = checked_at.astimezone(US_MARKET_ZONE).date()
        statement_rows = _statement_quarters(statements)
        events = _date_events(
            symbol,
            earnings_dates,
            checked_at=checked_at,
            market_date=market_date,
        )

        used_periods: set[dt.date] = set()
        merged = []
        for event in sorted(events, key=lambda item: item.report_date):
            if event.event_status is EventStatus.EXPECTED:
                merged.append(event)
                continue
            candidates = [
                row
                for row in statement_rows
                if row["fiscal_period_end"] not in used_periods
                and 0 <= (event.report_date - row["fiscal_period_end"]).days <= 200
            ]
            statement = max(candidates, key=lambda row: row["fiscal_period_end"]) if candidates else None
            if statement is None:
                merged.append(event)
                continue
            period = statement["fiscal_period_end"]
            used_periods.add(period)
            merged.append(
                replace(
                    event,
                    event_key=f"FPE:{period.isoformat()}",
                    fiscal_period_end=period,
                    statement_diluted_eps=statement.get("diluted_eps"),
                    statement_basic_eps=statement.get("basic_eps"),
                    revenue=statement.get("revenue"),
                )
            )

        if not any(event.event_status is EventStatus.EXPECTED for event in merged):
            expected_dt = _calendar_earnings_datetime(calendar)
            if expected_dt is not None:
                report_date, report_datetime_utc, timing = _report_datetime_parts(
                    expected_dt
                )
                if report_date >= market_date:
                    merged.append(
                        EarningsEvent(
                            symbol=symbol,
                            event_key="NEXT_EXPECTED",
                            report_date=report_date,
                            report_datetime_utc=report_datetime_utc,
                            event_status=EventStatus.EXPECTED,
                            report_timing=timing,
                            is_date_estimated=True,
                            source="yahoo",
                            source_updated_at=checked_at,
                        )
                    )

        # Yahoo may return several future estimates. The app intentionally
        # stores one mutable current expectation under a stable identity.
        expected = sorted(
            (event for event in merged if event.event_status is EventStatus.EXPECTED),
            key=lambda event: event.report_date,
        )
        reported = [
            event for event in merged if event.event_status is EventStatus.REPORTED
        ]
        if expected:
            expected_event = replace(expected[0], event_key="NEXT_EXPECTED")
            reported.append(expected_event)

        # A fiscal-period match is deterministic; unmatched reports use their
        # report date and an occurrence suffix to avoid collisions.
        deduped = {}
        for event in reported:
            key = event.event_key
            if key in deduped and event.event_status is EventStatus.REPORTED:
                suffix = 2
                while f"{key}:{suffix}" in deduped:
                    suffix += 1
                event = replace(event, event_key=f"{key}:{suffix}")
            deduped[event.event_key] = event
        return enrich_earnings_history(deduped.values())


def _date_events(
    symbol: str,
    frame,
    *,
    checked_at: dt.datetime,
    market_date: dt.date,
) -> list[EarningsEvent]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    data = frame.copy()
    if "Earnings Date" in data.columns:
        date_values = data.pop("Earnings Date")
    else:
        date_values = data.index
    columns = {_normalized_key(column): column for column in data.columns}
    estimate_column = columns.get("epsestimate") or columns.get("estimatedeps")
    reported_column = columns.get("reportedeps")
    surprise_column = columns.get("surprise") or columns.get("surprisepercent")
    events = []
    occurrence_by_date: dict[dt.date, int] = {}
    for position, (_, row) in enumerate(data.iterrows()):
        try:
            raw_date = date_values.iloc[position] if hasattr(date_values, "iloc") else date_values[position]
            report_date, report_datetime_utc, timing = _report_datetime_parts(raw_date)
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
        estimated_eps = _finite(row.get(estimate_column)) if estimate_column else None
        reported_eps = _finite(row.get(reported_column)) if reported_column else None
        is_expected = reported_eps is None and report_date >= market_date
        status = EventStatus.EXPECTED if is_expected else EventStatus.REPORTED
        occurrence_by_date[report_date] = occurrence_by_date.get(report_date, 0) + 1
        event_key = (
            "NEXT_EXPECTED"
            if is_expected
            else f"REPORT:{report_date.isoformat()}:{occurrence_by_date[report_date]}"
        )
        eps_surprise = (
            reported_eps - estimated_eps
            if reported_eps is not None and estimated_eps is not None
            else None
        )
        surprise_pct = _finite(row.get(surprise_column)) if surprise_column else None
        # yfinance 0.2.44 normalizes its ``Surprise(%)`` column to a fraction.
        if surprise_pct is not None and surprise_column and "surprise" in _normalized_key(surprise_column):
            surprise_pct *= 100.0
        events.append(
            EarningsEvent(
                symbol=symbol,
                event_key=event_key,
                report_date=report_date,
                report_datetime_utc=report_datetime_utc,
                event_status=status,
                report_timing=timing,
                is_date_estimated=is_expected,
                reported_eps=reported_eps,
                estimated_eps=estimated_eps,
                eps_surprise=eps_surprise,
                eps_surprise_pct=surprise_pct,
                source="yahoo",
                source_updated_at=checked_at,
            )
        )
    return events


def _statement_quarters(frame) -> list[dict]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    data = frame.copy()
    row_keys = {_normalized_key(index): index for index in data.index}
    needed = {"dilutedeps", "basiceps", "totalrevenue"}
    if not needed.intersection(row_keys):
        column_keys = {_normalized_key(column): column for column in data.columns}
        if needed.intersection(column_keys):
            data = data.transpose()
            row_keys = {_normalized_key(index): index for index in data.index}
    diluted_row = row_keys.get("dilutedeps")
    basic_row = row_keys.get("basiceps")
    revenue_row = row_keys.get("totalrevenue")
    if revenue_row is None:
        revenue_row = row_keys.get("operatingrevenue")
    rows = []
    for column in data.columns:
        try:
            period = pd.Timestamp(column).date()
        except (TypeError, ValueError, OverflowError):
            continue
        rows.append(
            {
                "fiscal_period_end": period,
                "diluted_eps": _finite(data.at[diluted_row, column]) if diluted_row is not None else None,
                "basic_eps": _finite(data.at[basic_row, column]) if basic_row is not None else None,
                "revenue": _finite(data.at[revenue_row, column]) if revenue_row is not None else None,
            }
        )
    return sorted(rows, key=lambda row: row["fiscal_period_end"])


def _report_datetime_parts(value: object) -> tuple[dt.date, dt.datetime, ReportTiming]:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(US_MARKET_ZONE)
    local = timestamp.tz_convert(US_MARKET_ZONE)
    if local.hour < 12:
        timing = ReportTiming.BMO
    elif local.hour >= 16:
        timing = ReportTiming.AMC
    else:
        timing = ReportTiming.UNKNOWN
    utc_value = local.tz_convert("UTC").to_pydatetime()
    return local.date(), utc_value, timing


def _calendar_earnings_datetime(calendar) -> Optional[object]:
    if calendar is None:
        return None
    if isinstance(calendar, pd.DataFrame):
        if calendar.empty:
            return None
        if "Earnings Date" in calendar.index:
            value = calendar.loc["Earnings Date"].iloc[0]
        elif "Earnings Date" in calendar.columns:
            value = calendar["Earnings Date"].iloc[0]
        else:
            return None
    elif isinstance(calendar, dict):
        value = calendar.get("Earnings Date")
        if value is None:
            value = calendar.get("earningsDate")
    else:
        return None
    if isinstance(value, (list, tuple, pd.Series, pd.Index)):
        value = value[0] if len(value) else None
    if value is None:
        return None
    try:
        pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value
