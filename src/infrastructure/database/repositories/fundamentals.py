"""Persistence for normalized stock profiles and earnings events."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import weakref
from dataclasses import asdict
from typing import Iterable, Mapping, Optional, Sequence

from sqlalchemy import MetaData, delete, select, update
from sqlalchemy.engine import Engine

from src.core.chart_fundamentals import (
    EarningsEvent,
    EventStatus,
    GrowthStatus,
    ProfileStatus,
    ReportTiming,
    StockProfile,
    canonical_symbol,
)
from src.infrastructure.database.schema import (
    _get_earnings_events_table,
    _get_fundamental_sync_state_table,
    _get_stock_profiles_table,
)
from src.infrastructure.database.sql_helpers import _execute_bulk_upsert
from src.infrastructure.database.time_utils import _utcnow_naive


PROFILE_DATASET = "PROFILE"
EARNINGS_DATASET = "EARNINGS"

_table_cache: weakref.WeakKeyDictionary[Engine, tuple] = weakref.WeakKeyDictionary()
_table_cache_lock = threading.Lock()


def _naive_utc(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def ensure_fundamental_tables(engine: Engine) -> None:
    _fundamental_tables(engine)


def _fundamental_tables(engine: Engine):
    with _table_cache_lock:
        cached = _table_cache.get(engine)
        if cached is not None:
            return cached
        metadata = MetaData()
        cached = (
            _get_stock_profiles_table(metadata),
            _get_earnings_events_table(metadata),
            _get_fundamental_sync_state_table(metadata),
        )
        metadata.create_all(engine)
        _table_cache[engine] = cached
        return cached


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _profile_record(profile: StockProfile) -> dict:
    return {
        "symbol": canonical_symbol(profile.symbol),
        "provider_symbol": profile.provider_symbol,
        "company_name": profile.company_name,
        "short_name": profile.short_name,
        "quote_type": profile.quote_type,
        "exchange": profile.exchange,
        "market": profile.market,
        "currency": profile.currency,
        "country": profile.country,
        "sector_name": profile.sector_name,
        "sector_key": profile.sector_key,
        "industry_name": profile.industry_name,
        "industry_key": profile.industry_key,
        "category": profile.category,
        "fund_family": profile.fund_family,
        "profile_status": str(_enum_value(profile.profile_status)),
        "source": profile.source,
        "last_checked_at": _naive_utc(profile.last_checked_at),
        "last_successful_sync_at": _naive_utc(profile.last_successful_sync_at),
        "updated_at": _naive_utc(profile.updated_at),
    }


_PROFILE_CONTENT_COLUMNS = (
    "provider_symbol",
    "company_name",
    "short_name",
    "quote_type",
    "exchange",
    "market",
    "currency",
    "country",
    "sector_name",
    "sector_key",
    "industry_name",
    "industry_key",
    "category",
    "fund_family",
    "profile_status",
    "source",
)


def upsert_stock_profile(engine: Engine, profile: StockProfile) -> bool:
    """Persist a profile and advance its mirror revision only when content changed."""
    table = _fundamental_tables(engine)[0]
    record = _profile_record(profile)
    now = _naive_utc(profile.updated_at) or _utcnow_naive()
    with engine.begin() as conn:
        existing = conn.execute(
            select(table).where(table.c.symbol == record["symbol"])
        ).mappings().first()
        if existing is not None:
            changed = any(existing.get(name) != record.get(name) for name in _PROFILE_CONTENT_COLUMNS)
            if not changed:
                conn.execute(
                    update(table)
                    .where(table.c.symbol == record["symbol"])
                    .values(
                        last_checked_at=record["last_checked_at"],
                        last_successful_sync_at=(
                            record["last_successful_sync_at"]
                            or existing.get("last_successful_sync_at")
                        ),
                    )
                )
                return False
            record["created_at"] = existing.get("created_at") or now
        else:
            record["created_at"] = now
        record["updated_at"] = now
        _execute_bulk_upsert(
            conn, table, [record], ("symbol",), engine.dialect.name
        )
    return True


def seed_stock_profiles(
    engine: Engine,
    profiles: Iterable[StockProfile],
) -> int:
    """Insert the complete basic universe without degrading enriched rows.

    KIS master data is the reliable offline baseline for symbol, company,
    exchange, and currency.  Yahoo enrichment may later add sector/industry.
    Re-seeding may update another KIS seed or replace an unavailable provider
    placeholder, but it must never overwrite a usable Yahoo-enriched profile.
    """

    table = _fundamental_tables(engine)[0]
    profiles_by_symbol = {
        canonical_symbol(profile.symbol): profile
        for profile in profiles
        if canonical_symbol(profile.symbol)
    }
    if not profiles_by_symbol:
        return 0

    with engine.begin() as conn:
        existing_rows = conn.execute(select(table)).mappings().all()
        existing_by_symbol = {
            str(row["symbol"]): row for row in existing_rows
        }
        records = []
        for symbol, profile in profiles_by_symbol.items():
            record = _profile_record(profile)
            existing = existing_by_symbol.get(symbol)
            if existing is not None:
                existing_source = str(existing.get("source") or "").lower()
                existing_status = str(existing.get("profile_status") or "").upper()
                may_replace = (
                    existing_source == "kis_master"
                    or existing_status == ProfileStatus.UNAVAILABLE.value
                )
                if not may_replace:
                    continue
                if not any(
                    existing.get(name) != record.get(name)
                    for name in _PROFILE_CONTENT_COLUMNS
                ):
                    continue
                record["created_at"] = (
                    existing.get("created_at")
                    or _naive_utc(profile.updated_at)
                    or _utcnow_naive()
                )
            else:
                record["created_at"] = (
                    _naive_utc(profile.updated_at) or _utcnow_naive()
                )
            records.append(record)

        _execute_bulk_upsert(
            conn,
            table,
            records,
            ("symbol",),
            engine.dialect.name,
        )
    return len(records)


def load_stock_profile(engine: Engine, symbol: str) -> Optional[StockProfile]:
    table = _fundamental_tables(engine)[0]
    symbol = canonical_symbol(symbol)
    with engine.connect() as conn:
        row = conn.execute(
            select(table).where(table.c.symbol == symbol)
        ).mappings().first()
    if row is None:
        return None
    return StockProfile(
        symbol=row["symbol"],
        provider_symbol=row["provider_symbol"],
        company_name=row["company_name"],
        short_name=row["short_name"],
        quote_type=row["quote_type"],
        exchange=row["exchange"],
        market=row["market"],
        currency=row["currency"],
        country=row["country"],
        sector_name=row["sector_name"],
        sector_key=row["sector_key"],
        industry_name=row["industry_name"],
        industry_key=row["industry_key"],
        category=row["category"],
        fund_family=row["fund_family"],
        profile_status=ProfileStatus(row["profile_status"]),
        source=row["source"],
        last_checked_at=row["last_checked_at"],
        last_successful_sync_at=row["last_successful_sync_at"],
        updated_at=row["updated_at"],
    )


def _event_record(event: EarningsEvent) -> dict:
    return {
        "symbol": canonical_symbol(event.symbol),
        "event_key": event.event_key,
        "report_date": event.report_date,
        "report_datetime_utc": _naive_utc(event.report_datetime_utc),
        "fiscal_period_end": event.fiscal_period_end,
        "event_status": str(_enum_value(event.event_status)),
        "report_timing": str(_enum_value(event.report_timing)),
        "is_date_estimated": bool(event.is_date_estimated),
        "reported_eps": event.reported_eps,
        "estimated_eps": event.estimated_eps,
        "statement_diluted_eps": event.statement_diluted_eps,
        "statement_basic_eps": event.statement_basic_eps,
        "eps_basis": event.eps_basis,
        "eps_surprise": event.eps_surprise,
        "eps_surprise_pct": event.eps_surprise_pct,
        "revenue": event.revenue,
        "estimated_revenue": event.estimated_revenue,
        "eps_yoy_growth_pct": event.eps_yoy_growth_pct,
        "previous_eps_yoy_growth_pct": event.previous_eps_yoy_growth_pct,
        "eps_growth_status": str(_enum_value(event.eps_growth_status)),
        "previous_eps_growth_status": str(
            _enum_value(event.previous_eps_growth_status)
        ),
        "revenue_yoy_growth_pct": event.revenue_yoy_growth_pct,
        "previous_revenue_yoy_growth_pct": event.previous_revenue_yoy_growth_pct,
        "ttm_eps": event.ttm_eps,
        "source": event.source,
        "source_updated_at": _naive_utc(event.source_updated_at),
    }


_EVENT_CONTENT_COLUMNS = (
    "report_date",
    "report_datetime_utc",
    "fiscal_period_end",
    "event_status",
    "report_timing",
    "is_date_estimated",
    "reported_eps",
    "estimated_eps",
    "statement_diluted_eps",
    "statement_basic_eps",
    "eps_basis",
    "eps_surprise",
    "eps_surprise_pct",
    "revenue",
    "estimated_revenue",
    "eps_yoy_growth_pct",
    "previous_eps_yoy_growth_pct",
    "eps_growth_status",
    "previous_eps_growth_status",
    "revenue_yoy_growth_pct",
    "previous_revenue_yoy_growth_pct",
    "ttm_eps",
    "source",
)


def upsert_earnings_events(
    engine: Engine,
    events: Iterable[EarningsEvent],
    *,
    replace_expected: bool = False,
    symbol: Optional[str] = None,
) -> int:
    """Upsert corrections while permanently retaining older reported history."""
    table = _fundamental_tables(engine)[1]
    records = [_event_record(event) for event in events]
    if not records and not replace_expected:
        return 0
    symbols = {record["symbol"] for record in records}
    if symbol:
        symbols.add(canonical_symbol(symbol))
    if replace_expected and len(symbols) != 1:
        raise ValueError("replace_expected requires events for exactly one symbol")
    now = _utcnow_naive()
    changed_records = []
    removed = 0
    removed_aliases = 0
    with engine.begin() as conn:
        for record in records:
            if (
                record["event_status"] == EventStatus.REPORTED.value
                and str(record["event_key"]).startswith("FPE:")
            ):
                removed_aliases += int(
                    conn.execute(
                        delete(table).where(
                            table.c.symbol == record["symbol"],
                            table.c.report_date == record["report_date"],
                            table.c.event_status == EventStatus.REPORTED.value,
                            table.c.event_key != record["event_key"],
                            table.c.event_key.like("REPORT:%"),
                        )
                    ).rowcount
                    or 0
                )
            existing = conn.execute(
                select(table).where(
                    table.c.symbol == record["symbol"],
                    table.c.event_key == record["event_key"],
                )
            ).mappings().first()
            if existing is not None and not any(
                existing.get(name) != record.get(name)
                for name in _EVENT_CONTENT_COLUMNS
            ):
                continue
            record["created_at"] = (
                existing.get("created_at") if existing is not None else None
            ) or now
            record["updated_at"] = now
            changed_records.append(record)
        if changed_records:
            _execute_bulk_upsert(
                conn,
                table,
                changed_records,
                ("symbol", "event_key"),
                engine.dialect.name,
            )
        if replace_expected:
            symbol = next(iter(symbols))
            expected_keys = {
                record["event_key"]
                for record in records
                if record["event_status"] == EventStatus.EXPECTED.value
            }
            stale = delete(table).where(
                table.c.symbol == symbol,
                table.c.event_status == EventStatus.EXPECTED.value,
            )
            if expected_keys:
                stale = stale.where(table.c.event_key.not_in(expected_keys))
            removed = int(conn.execute(stale).rowcount or 0)
    return len(changed_records) + removed + removed_aliases


def load_earnings_events(
    engine: Engine,
    symbol: str,
    *,
    through: Optional[dt.date] = None,
) -> tuple[EarningsEvent, ...]:
    table = _fundamental_tables(engine)[1]
    symbol = canonical_symbol(symbol)
    stmt = select(table).where(table.c.symbol == symbol)
    if through is not None:
        stmt = stmt.where(table.c.report_date <= through)
    stmt = stmt.order_by(table.c.report_date, table.c.event_key)
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return tuple(_event_from_row(row) for row in rows)


def _event_from_row(row: Mapping[str, object]) -> EarningsEvent:
    return EarningsEvent(
        symbol=str(row["symbol"]),
        event_key=str(row["event_key"]),
        report_date=row["report_date"],
        report_datetime_utc=row["report_datetime_utc"],
        fiscal_period_end=row["fiscal_period_end"],
        event_status=EventStatus(str(row["event_status"])),
        report_timing=ReportTiming(str(row["report_timing"])),
        is_date_estimated=bool(row["is_date_estimated"]),
        reported_eps=row["reported_eps"],
        estimated_eps=row["estimated_eps"],
        statement_diluted_eps=row["statement_diluted_eps"],
        statement_basic_eps=row["statement_basic_eps"],
        eps_basis=row["eps_basis"],
        eps_surprise=row["eps_surprise"],
        eps_surprise_pct=row["eps_surprise_pct"],
        revenue=row["revenue"],
        estimated_revenue=row["estimated_revenue"],
        eps_yoy_growth_pct=row["eps_yoy_growth_pct"],
        previous_eps_yoy_growth_pct=row["previous_eps_yoy_growth_pct"],
        eps_growth_status=GrowthStatus(str(row["eps_growth_status"])),
        previous_eps_growth_status=GrowthStatus(
            str(row["previous_eps_growth_status"])
        ),
        revenue_yoy_growth_pct=row["revenue_yoy_growth_pct"],
        previous_revenue_yoy_growth_pct=row[
            "previous_revenue_yoy_growth_pct"
        ],
        ttm_eps=row["ttm_eps"],
        source=str(row["source"]),
        source_updated_at=row["source_updated_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def normalized_payload_fingerprint(payload: object) -> str:
    def default(value: object) -> str:
        if isinstance(value, (dt.date, dt.datetime)):
            return value.isoformat()
        return str(_enum_value(value))

    if hasattr(payload, "__dataclass_fields__"):
        payload = asdict(payload)
    encoded = json.dumps(
        payload,
        default=default,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_fundamental_sync_state(
    engine: Engine,
    *,
    symbol: str,
    dataset: str,
    status: ProfileStatus | str,
    source: str,
    checked_at: dt.datetime,
    successful_at: Optional[dt.datetime],
    payload_fingerprint: Optional[str],
    last_error: Optional[str] = None,
) -> bool:
    table = _fundamental_tables(engine)[2]
    symbol = canonical_symbol(symbol)
    dataset = str(dataset).strip().upper()
    now = _naive_utc(checked_at) or _utcnow_naive()
    record = {
        "symbol": symbol,
        "dataset": dataset,
        "status": str(_enum_value(status)),
        "source": source,
        "payload_fingerprint": payload_fingerprint,
        "last_error": str(last_error or "")[:500] or None,
        "last_checked_at": now,
        "last_successful_sync_at": _naive_utc(successful_at),
    }
    content_columns = (
        "status",
        "source",
        "payload_fingerprint",
        "last_error",
    )
    with engine.begin() as conn:
        existing = conn.execute(
            select(table).where(
                table.c.symbol == symbol, table.c.dataset == dataset
            )
        ).mappings().first()
        if existing is not None and record["last_successful_sync_at"] is None:
            record["last_successful_sync_at"] = existing.get(
                "last_successful_sync_at"
            )
        changed = existing is None or any(
            existing.get(name) != record.get(name) for name in content_columns
        )
        if existing is not None and not changed:
            conn.execute(
                update(table)
                .where(table.c.symbol == symbol, table.c.dataset == dataset)
                .values(
                    last_checked_at=now,
                    last_successful_sync_at=(
                        record["last_successful_sync_at"]
                        or existing.get("last_successful_sync_at")
                    ),
                )
            )
            return False
        record["created_at"] = (
            existing.get("created_at") if existing is not None else None
        ) or now
        record["updated_at"] = now
        _execute_bulk_upsert(
            conn,
            table,
            [record],
            ("symbol", "dataset"),
            engine.dialect.name,
        )
    return True


def load_fundamental_sync_state(
    engine: Engine, symbol: str, dataset: str
) -> Optional[dict]:
    table = _fundamental_tables(engine)[2]
    with engine.connect() as conn:
        row = conn.execute(
            select(table).where(
                table.c.symbol == canonical_symbol(symbol),
                table.c.dataset == str(dataset).strip().upper(),
            )
        ).mappings().first()
    return dict(row) if row is not None else None


def load_fundamental_sync_states(
    engine: Engine, dataset: str
) -> dict[str, dict]:
    """Load one dataset's freshness rows in one query for universe refreshes."""

    table = _fundamental_tables(engine)[2]
    dataset = str(dataset).strip().upper()
    with engine.connect() as conn:
        rows = conn.execute(
            select(table).where(table.c.dataset == dataset)
        ).mappings().all()
    return {str(row["symbol"]): dict(row) for row in rows}
