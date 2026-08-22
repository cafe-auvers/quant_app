"""Typed stock-profile and earnings models plus point-in-time calculations."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Iterable, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


US_MARKET_ZONE = ZoneInfo("America/New_York")


class ProfileStatus(str, Enum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class EventStatus(str, Enum):
    REPORTED = "REPORTED"
    EXPECTED = "EXPECTED"


class ReportTiming(str, Enum):
    BMO = "BMO"
    AMC = "AMC"
    UNKNOWN = "UNKNOWN"


class GrowthStatus(str, Enum):
    NORMAL = "NORMAL"
    MISSING = "MISSING"
    ZERO_BASE = "ZERO_BASE"
    TURNAROUND = "TURNAROUND"
    LOSS = "LOSS"
    NEGATIVE_NOT_MEANINGFUL = "NEGATIVE_NOT_MEANINGFUL"


@dataclass(frozen=True)
class GrowthResult:
    value: Optional[float]
    status: GrowthStatus
    display_token: str


@dataclass(frozen=True)
class StockProfile:
    symbol: str
    company_name: str
    source: str
    last_checked_at: dt.datetime
    updated_at: dt.datetime
    provider_symbol: Optional[str] = None
    short_name: Optional[str] = None
    quote_type: Optional[str] = None
    exchange: Optional[str] = None
    market: Optional[str] = None
    currency: Optional[str] = None
    country: Optional[str] = None
    sector_name: Optional[str] = None
    sector_key: Optional[str] = None
    industry_name: Optional[str] = None
    industry_key: Optional[str] = None
    category: Optional[str] = None
    fund_family: Optional[str] = None
    profile_status: ProfileStatus = ProfileStatus.OK
    last_successful_sync_at: Optional[dt.datetime] = None


@dataclass(frozen=True)
class EarningsEvent:
    symbol: str
    event_key: str
    report_date: dt.date
    event_status: EventStatus
    source: str
    source_updated_at: dt.datetime
    report_datetime_utc: Optional[dt.datetime] = None
    fiscal_period_end: Optional[dt.date] = None
    report_timing: ReportTiming = ReportTiming.UNKNOWN
    is_date_estimated: bool = False
    reported_eps: Optional[float] = None
    estimated_eps: Optional[float] = None
    statement_diluted_eps: Optional[float] = None
    statement_basic_eps: Optional[float] = None
    eps_basis: Optional[str] = None
    eps_surprise: Optional[float] = None
    eps_surprise_pct: Optional[float] = None
    revenue: Optional[float] = None
    estimated_revenue: Optional[float] = None
    eps_yoy_growth_pct: Optional[float] = None
    previous_eps_yoy_growth_pct: Optional[float] = None
    eps_growth_status: GrowthStatus = GrowthStatus.MISSING
    previous_eps_growth_status: GrowthStatus = GrowthStatus.MISSING
    revenue_yoy_growth_pct: Optional[float] = None
    previous_revenue_yoy_growth_pct: Optional[float] = None
    ttm_eps: Optional[float] = None
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None

    @property
    def selected_eps(self) -> Optional[float]:
        field_name = {
            "REPORTED": "reported_eps",
            "DILUTED": "statement_diluted_eps",
            "BASIC": "statement_basic_eps",
        }.get(str(self.eps_basis or "").upper())
        if not field_name:
            return None
        return getattr(self, field_name)


@dataclass(frozen=True)
class EarningsLinePoint:
    date: dt.date
    value: float


@dataclass(frozen=True)
class UpcomingEarnings:
    event: EarningsEvent
    next_earnings_date: dt.date
    days_to_earnings: int
    has_earnings_within_14d: bool
    report_timing: ReportTiming
    is_date_estimated: bool

    @property
    def badge_text(self) -> str:
        if self.days_to_earnings == 0:
            day_text = "Today"
        elif self.days_to_earnings == 1:
            day_text = "Tomorrow"
        else:
            day_text = f"{self.days_to_earnings}d"
        timing = (
            f" {self.report_timing.value}"
            if self.report_timing is not ReportTiming.UNKNOWN
            else ""
        )
        return f"E {day_text}{timing}"


@dataclass(frozen=True)
class ChartFundamentalContext:
    symbol: str
    stock_profile: Optional[StockProfile] = None
    earnings_events: Tuple[EarningsEvent, ...] = ()
    next_earnings: Optional[UpcomingEarnings] = None
    earnings_line: Tuple[EarningsLinePoint, ...] = ()
    revision_token: str = "empty"


@dataclass(frozen=True)
class StockProfileProviderResult:
    symbol: str
    profile: StockProfile
    status: ProfileStatus
    errors: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EarningsProviderResult:
    symbol: str
    events: Tuple[EarningsEvent, ...]
    status: ProfileStatus
    checked_at: dt.datetime
    source: str = "yahoo"
    errors: Tuple[str, ...] = ()


def canonical_symbol(value: object) -> str:
    """Return the app-owned symbol, stripping only a display exchange prefix."""
    symbol = str(value or "").strip().upper()
    if ":" in symbol:
        symbol = symbol.split(":", 1)[1]
    return symbol


def clean_optional_text(value: object) -> Optional[str]:
    """Normalize provider text without leaking placeholder values to storage/UI."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "n/a", "na", "nan", "nat"}:
        return None
    return text


def _finite_number(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _rounded_growth_token(value: float) -> str:
    rounded = int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if rounded > 999:
        return "999+"
    if rounded < -999:
        return "-999+"
    return str(rounded)


def calculate_eps_growth(current_eps: object, prior_year_eps: object) -> GrowthResult:
    """Calculate honest YoY EPS growth, explicitly classifying non-meaningful cases."""
    current = _finite_number(current_eps)
    prior = _finite_number(prior_year_eps)
    if current is None or prior is None:
        return GrowthResult(None, GrowthStatus.MISSING, "N/A")
    if prior == 0:
        return GrowthResult(None, GrowthStatus.ZERO_BASE, "N/A")
    if prior < 0 < current:
        return GrowthResult(None, GrowthStatus.TURNAROUND, "TURN")
    if prior > 0 > current:
        return GrowthResult(None, GrowthStatus.LOSS, "LOSS")
    if prior < 0 and current <= 0:
        return GrowthResult(None, GrowthStatus.NEGATIVE_NOT_MEANINGFUL, "N/M")
    value = (current - prior) / abs(prior) * 100.0
    return GrowthResult(value, GrowthStatus.NORMAL, _rounded_growth_token(value))


def calculate_revenue_growth(current: object, prior_year: object) -> Optional[float]:
    current_value = _finite_number(current)
    prior_value = _finite_number(prior_year)
    if current_value is None or prior_value in (None, 0):
        return None
    return (current_value - prior_value) / abs(prior_value) * 100.0


def growth_display_token(
    value: Optional[float], status: GrowthStatus | str | None
) -> str:
    try:
        normalized_status = GrowthStatus(str(getattr(status, "value", status)))
    except ValueError:
        normalized_status = GrowthStatus.MISSING
    if normalized_status is GrowthStatus.NORMAL and value is not None:
        return _rounded_growth_token(value)
    return {
        GrowthStatus.TURNAROUND: "TURN",
        GrowthStatus.LOSS: "LOSS",
        GrowthStatus.NEGATIVE_NOT_MEANINGFUL: "N/M",
    }.get(normalized_status, "N/A")


def format_compact_growth_pair(event: EarningsEvent) -> str:
    current = growth_display_token(event.eps_yoy_growth_pct, event.eps_growth_status)
    previous = growth_display_token(
        event.previous_eps_yoy_growth_pct, event.previous_eps_growth_status
    )
    if current == "N/A" and previous == "N/A":
        return ""
    return f"{current}/{previous}"


def _event_sort_date(event: EarningsEvent) -> dt.date:
    return event.fiscal_period_end or event.report_date


def _quarter_gap_is_valid(older: EarningsEvent, newer: EarningsEvent) -> bool:
    gap = (_event_sort_date(newer) - _event_sort_date(older)).days
    return 45 <= gap <= 140


def _consecutive(events: Sequence[EarningsEvent]) -> bool:
    return all(
        _quarter_gap_is_valid(older, newer)
        for older, newer in zip(events, events[1:])
    )


def _has_basis_window(
    events: Sequence[EarningsEvent], field_name: str, size: int
) -> bool:
    available = [event for event in events if _finite_number(getattr(event, field_name)) is not None]
    return any(
        _consecutive(available[index : index + size])
        for index in range(max(0, len(available) - size + 1))
    )


def select_consistent_eps_basis(events: Iterable[EarningsEvent]) -> Optional[str]:
    """Choose one EPS basis for the complete sequence; comparison quarters never mix."""
    reported = sorted(
        (event for event in events if event.event_status is EventStatus.REPORTED),
        key=_event_sort_date,
    )
    basis_fields = (
        ("REPORTED", "reported_eps"),
        ("DILUTED", "statement_diluted_eps"),
        ("BASIC", "statement_basic_eps"),
    )
    for basis, field_name in basis_fields:
        if _has_basis_window(reported, field_name, 5):
            return basis
    # Four quarters still support a TTM line even when a YoY comparison is not
    # yet possible. Preserve the provider preference order.
    for basis, field_name in basis_fields:
        if _has_basis_window(reported, field_name, 4):
            return basis
    return None


def enrich_earnings_history(
    events: Iterable[EarningsEvent],
) -> Tuple[EarningsEvent, ...]:
    """Apply one EPS basis, growth pairs, revenue growth, and report-date TTM EPS."""
    all_events = list(events)
    reported = sorted(
        (event for event in all_events if event.event_status is EventStatus.REPORTED),
        key=_event_sort_date,
    )
    basis = select_consistent_eps_basis(reported)
    field_name = {
        "REPORTED": "reported_eps",
        "DILUTED": "statement_diluted_eps",
        "BASIC": "statement_basic_eps",
    }.get(basis or "")
    replacements = {}
    for index, event in enumerate(reported):
        current_growth = GrowthResult(None, GrowthStatus.MISSING, "N/A")
        previous_growth = GrowthResult(None, GrowthStatus.MISSING, "N/A")
        revenue_growth = None
        previous_revenue_growth = None
        ttm_eps = None
        if field_name and index >= 4 and _consecutive(reported[index - 4 : index + 1]):
            current_growth = calculate_eps_growth(
                getattr(event, field_name), getattr(reported[index - 4], field_name)
            )
            revenue_growth = calculate_revenue_growth(
                event.revenue, reported[index - 4].revenue
            )
        if field_name and index >= 5 and _consecutive(reported[index - 5 : index]):
            previous_growth = calculate_eps_growth(
                getattr(reported[index - 1], field_name),
                getattr(reported[index - 5], field_name),
            )
            previous_revenue_growth = calculate_revenue_growth(
                reported[index - 1].revenue, reported[index - 5].revenue
            )
        if field_name and index >= 3:
            ttm_window = reported[index - 3 : index + 1]
            values = [_finite_number(getattr(item, field_name)) for item in ttm_window]
            if _consecutive(ttm_window) and all(value is not None for value in values):
                ttm_eps = float(sum(value for value in values if value is not None))
        replacements[event.event_key] = replace(
            event,
            eps_basis=basis,
            eps_yoy_growth_pct=current_growth.value,
            previous_eps_yoy_growth_pct=previous_growth.value,
            eps_growth_status=current_growth.status,
            previous_eps_growth_status=previous_growth.status,
            revenue_yoy_growth_pct=revenue_growth,
            previous_revenue_yoy_growth_pct=previous_revenue_growth,
            ttm_eps=ttm_eps,
        )
    enriched = [replacements.get(event.event_key, event) for event in all_events]
    return tuple(sorted(enriched, key=lambda item: (item.report_date, item.event_key)))


def _market_date(value: dt.date | dt.datetime | None = None) -> dt.date:
    if value is None:
        return dt.datetime.now(dt.timezone.utc).astimezone(US_MARKET_ZONE).date()
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(US_MARKET_ZONE).date()
    return value


def next_upcoming_earnings(
    events: Iterable[EarningsEvent],
    *,
    as_of: dt.date | dt.datetime | None = None,
    horizon_days: int = 14,
) -> Optional[UpcomingEarnings]:
    as_of_date = _market_date(as_of)
    expected = sorted(
        (
            event
            for event in events
            if event.event_status is EventStatus.EXPECTED
            and event.report_date >= as_of_date
        ),
        key=lambda event: event.report_date,
    )
    if not expected:
        return None
    event = expected[0]
    days = (event.report_date - as_of_date).days
    return UpcomingEarnings(
        event=event,
        next_earnings_date=event.report_date,
        days_to_earnings=days,
        has_earnings_within_14d=0 <= days <= max(0, int(horizon_days)),
        report_timing=event.report_timing,
        is_date_estimated=event.is_date_estimated,
    )


def build_ttm_earnings_line(
    events: Iterable[EarningsEvent], chart_dates: Iterable[object]
) -> Tuple[EarningsLinePoint, ...]:
    """Forward-fill TTM EPS from report dates, never fiscal-period dates."""
    reported = sorted(
        (event for event in events if event.event_status is EventStatus.REPORTED),
        key=lambda event: (event.report_date, event.event_key),
    )
    bases = {event.eps_basis for event in reported if event.eps_basis}
    if len(bases) > 1:
        raise ValueError("TTM EPS sequence contains inconsistent EPS bases")
    if not reported or not bases:
        return ()

    # Loaded rows normally carry the point-in-time TTM value. Re-enriching
    # also supports callers/tests that supplied a raw but consistent sequence.
    if not any(event.ttm_eps is not None for event in reported):
        reported = list(enrich_earnings_history(reported))
    changes = [
        (event.report_date, float(event.ttm_eps))
        for event in reported
        if event.ttm_eps is not None
    ]
    if not changes:
        return ()

    normalized_dates = sorted(
        {
            value.date() if isinstance(value, dt.datetime) else value
            for value in (
                _coerce_chart_date(item) for item in chart_dates
            )
            if value is not None
        }
    )
    result = []
    change_index = 0
    current_value: Optional[float] = None
    for chart_date in normalized_dates:
        while change_index < len(changes) and changes[change_index][0] <= chart_date:
            current_value = changes[change_index][1]
            change_index += 1
        if current_value is not None:
            result.append(EarningsLinePoint(chart_date, current_value))
    return tuple(result)


def _coerce_chart_date(value: object) -> Optional[dt.date | dt.datetime]:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return value
    try:
        # Avoid a pandas dependency in the core calculation module.
        converted = value.to_pydatetime()
    except (AttributeError, TypeError, ValueError):
        return None
    return converted
