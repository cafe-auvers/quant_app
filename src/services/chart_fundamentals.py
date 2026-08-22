"""Cached chart enrichment orchestration for profiles and earnings."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable, Iterable, Optional

import pandas as pd
from sqlalchemy.engine import Engine

from src.core.chart_fundamentals import (
    ChartFundamentalContext,
    EarningsProviderResult,
    ProfileStatus,
    StockProfile,
    StockProfileProviderResult,
    build_ttm_earnings_line,
    canonical_symbol,
    next_upcoming_earnings,
)
from src.infrastructure.database.repositories.fundamentals import (
    EARNINGS_DATASET,
    PROFILE_DATASET,
    ensure_fundamental_tables,
    load_earnings_events,
    load_fundamental_sync_state,
    load_fundamental_sync_states,
    load_stock_profile,
    normalized_payload_fingerprint,
    record_fundamental_sync_state,
    seed_stock_profiles,
    upsert_earnings_events,
    upsert_stock_profile,
)
from src.services.fundamental_providers import (
    EarningsProvider,
    StockProfileProvider,
    YahooEarningsProvider,
    YahooStockProfileProvider,
    YahooTickerClient,
    yahoo_provider_symbol,
)
from src.utils.data_loader import DEFAULT_UNIVERSE_CACHE, get_default_universe


logger = logging.getLogger(__name__)

PROFILE_FRESH_FOR = dt.timedelta(days=30)
UNAVAILABLE_FRESH_FOR = dt.timedelta(hours=24)
BULK_UNAVAILABLE_FRESH_FOR = dt.timedelta(days=7)
EARNINGS_FRESH_FOR = dt.timedelta(hours=24)
NEAR_EARNINGS_FRESH_FOR = dt.timedelta(hours=8)
KIS_PROFILE_SOURCE = "kis_master"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware_utc(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def seed_default_universe_stock_profiles(
    engine: Engine,
    *,
    universe: Optional[Iterable[str]] = None,
    cache_path: Path | str = DEFAULT_UNIVERSE_CACHE,
    now: Optional[dt.datetime] = None,
) -> int:
    """Seed one basic profile for every configured US-universe symbol.

    This path performs no Internet requests.  It makes the table complete
    from the locally cached KIS master even when Yahoo is unavailable; normal
    per-symbol refreshes can enrich sector and industry later.
    """

    checked_at = _aware_utc(now or _utcnow())
    assert checked_at is not None
    symbols = list(
        dict.fromkeys(
            canonical_symbol(symbol)
            for symbol in (
                universe
                if universe is not None
                else get_default_universe(max_symbols=None, refresh=False)
            )
            if canonical_symbol(symbol)
        )
    )
    rows_by_symbol = {}
    path = Path(cache_path)
    if path.exists():
        try:
            frame = pd.read_csv(path, dtype=str).fillna("")
            for row in frame.to_dict(orient="records"):
                symbol = canonical_symbol(row.get("Symbol"))
                if symbol:
                    rows_by_symbol[symbol] = row
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not read KIS profile seed cache: %s", exc)

    profiles = []
    for symbol in symbols:
        row = rows_by_symbol.get(symbol, {})
        company_name = str(row.get("Name") or "").strip() or symbol
        profiles.append(
            StockProfile(
                symbol=symbol,
                provider_symbol=yahoo_provider_symbol(symbol),
                company_name=company_name,
                quote_type="EQUITY",
                exchange=str(row.get("Exchange") or "").strip() or None,
                market="US",
                currency=str(row.get("Currency") or "").strip() or "USD",
                country="United States",
                profile_status=ProfileStatus.PARTIAL,
                source=KIS_PROFILE_SOURCE,
                last_checked_at=checked_at,
                last_successful_sync_at=checked_at,
                updated_at=checked_at,
            )
        )
    changed = seed_stock_profiles(engine, profiles)
    logger.info(
        "Stock profile universe seed complete: %d configured, %d inserted/updated",
        len(profiles),
        changed,
    )
    return changed


class ChartFundamentalService:
    """Load cached context synchronously and refresh provider data off-thread."""

    def __init__(
        self,
        engine: Engine,
        *,
        profile_provider: Optional[StockProfileProvider] = None,
        earnings_provider: Optional[EarningsProvider] = None,
        now: Callable[[], dt.datetime] = _utcnow,
    ):
        self.engine = engine
        self._now = now
        if profile_provider is None or earnings_provider is None:
            client = YahooTickerClient()
            profile_provider = profile_provider or YahooStockProfileProvider(client)
            earnings_provider = earnings_provider or YahooEarningsProvider(client)
        self.profile_provider = profile_provider
        self.earnings_provider = earnings_provider

    def load_chart_fundamental_context(
        self,
        symbol: str,
        *,
        chart_dates: Optional[Iterable[object]] = None,
        as_of: dt.date | dt.datetime | None = None,
        horizon_days: int = 14,
    ) -> ChartFundamentalContext:
        canonical = canonical_symbol(symbol)
        ensure_fundamental_tables(self.engine)
        profile = load_stock_profile(self.engine, canonical)
        events = load_earnings_events(self.engine, canonical)
        if profile is not None:
            logger.debug(
                "stock_profile_cache_hit symbol=%s cache_age=%s",
                canonical,
                self._now() - (_aware_utc(profile.last_checked_at) or self._now()),
            )
        if events:
            logger.debug(
                "earnings_cache_hit symbol=%s record_count=%d",
                canonical,
                len(events),
            )
        upcoming = next_upcoming_earnings(
            events, as_of=as_of, horizon_days=horizon_days
        )
        line = (
            build_ttm_earnings_line(events, chart_dates)
            if chart_dates is not None
            else ()
        )
        revision_parts = [canonical]
        if profile is not None:
            revision_parts.append(str(profile.updated_at))
        revision_parts.extend(
            f"{event.event_key}:{event.updated_at or event.source_updated_at}"
            for event in events
        )
        token = hashlib.sha256("|".join(revision_parts).encode("utf-8")).hexdigest()[:16]
        logger.debug(
            "chart_fundamental_context_loaded symbol=%s records=%d revision=%s",
            canonical,
            len(events),
            token,
        )
        return ChartFundamentalContext(
            symbol=canonical,
            stock_profile=profile,
            earnings_events=events,
            next_earnings=upcoming,
            earnings_line=line,
            revision_token=token,
        )

    @staticmethod
    def align_context_to_chart(
        context: ChartFundamentalContext, chart_dates: Iterable[object]
    ) -> ChartFundamentalContext:
        return replace(
            context,
            earnings_line=build_ttm_earnings_line(
                context.earnings_events, chart_dates
            ),
        )

    def refresh_required(
        self,
        symbol: str,
        *,
        as_of: dt.datetime | None = None,
        horizon_days: int = 14,
    ) -> bool:
        now = _aware_utc(as_of or self._now())
        assert now is not None
        profile_state = load_fundamental_sync_state(
            self.engine, symbol, PROFILE_DATASET
        )
        earnings_state = load_fundamental_sync_state(
            self.engine, symbol, EARNINGS_DATASET
        )
        profile_due = self._state_due(
            profile_state,
            now,
            normal_interval=PROFILE_FRESH_FOR,
        )
        events = load_earnings_events(self.engine, symbol)
        upcoming = next_upcoming_earnings(
            events, as_of=now, horizon_days=horizon_days
        )
        earnings_interval = (
            NEAR_EARNINGS_FRESH_FOR
            if upcoming is not None and upcoming.has_earnings_within_14d
            else EARNINGS_FRESH_FOR
        )
        earnings_due = self._state_due(
            earnings_state,
            now,
            normal_interval=earnings_interval,
        )
        if profile_due or earnings_due:
            logger.info(
                "chart_fundamental_context_refresh_scheduled symbol=%s profile_due=%s earnings_due=%s",
                canonical_symbol(symbol),
                profile_due,
                earnings_due,
            )
        return profile_due or earnings_due

    def profile_refresh_required(
        self,
        symbol: str,
        *,
        as_of: dt.datetime | None = None,
        unavailable_interval: dt.timedelta = UNAVAILABLE_FRESH_FOR,
    ) -> bool:
        now = _aware_utc(as_of or self._now())
        assert now is not None
        state = load_fundamental_sync_state(
            self.engine, symbol, PROFILE_DATASET
        )
        return self._state_due(
            state,
            now,
            normal_interval=PROFILE_FRESH_FOR,
            unavailable_interval=unavailable_interval,
        )

    @staticmethod
    def _state_due(
        state: Optional[dict],
        now: dt.datetime,
        *,
        normal_interval: dt.timedelta,
        unavailable_interval: dt.timedelta = UNAVAILABLE_FRESH_FOR,
    ) -> bool:
        if state is None:
            return True
        checked = _aware_utc(state.get("last_checked_at"))
        if checked is None:
            return True
        interval = (
            unavailable_interval
            if str(state.get("status", "")).upper() == ProfileStatus.UNAVAILABLE.value
            else normal_interval
        )
        return now - checked >= interval

    def refresh_symbol(self, symbol: str) -> ChartFundamentalContext:
        canonical = canonical_symbol(symbol)
        ensure_fundamental_tables(self.engine)
        self._refresh_profile(canonical)
        self._refresh_earnings(canonical)
        return self.load_chart_fundamental_context(canonical, as_of=self._now())

    def refresh_profile(self, symbol: str) -> ProfileStatus:
        """Refresh only the profile dataset for a bulk-universe job."""

        canonical = canonical_symbol(symbol)
        ensure_fundamental_tables(self.engine)
        return self._refresh_profile(canonical)

    def _refresh_profile(self, symbol: str) -> ProfileStatus:
        logger.info("stock_profile_sync_started symbol=%s", symbol)
        checked_at = self._now()
        try:
            result = self.profile_provider.fetch_stock_profile(symbol)
        except Exception as exc:  # replaceable provider boundary
            logger.warning("stock_profile_sync_failed symbol=%s error=%s", symbol, exc)
            record_fundamental_sync_state(
                self.engine,
                symbol=symbol,
                dataset=PROFILE_DATASET,
                status=ProfileStatus.UNAVAILABLE,
                source="yahoo",
                checked_at=checked_at,
                successful_at=None,
                payload_fingerprint=None,
                last_error=str(exc),
            )
            return ProfileStatus.UNAVAILABLE
        if not isinstance(result, StockProfileProviderResult):
            raise TypeError("stock profile provider returned an invalid result")
        existing = load_stock_profile(self.engine, symbol)
        if result.status is not ProfileStatus.UNAVAILABLE or existing is None:
            changed = upsert_stock_profile(self.engine, result.profile)
        else:
            changed = False
        payload = asdict(result.profile)
        for key in ("last_checked_at", "last_successful_sync_at", "updated_at"):
            payload.pop(key, None)
        fingerprint = normalized_payload_fingerprint(payload)
        record_fundamental_sync_state(
            self.engine,
            symbol=symbol,
            dataset=PROFILE_DATASET,
            status=result.status,
            source=result.profile.source,
            checked_at=result.profile.last_checked_at,
            successful_at=result.profile.last_successful_sync_at,
            payload_fingerprint=fingerprint,
            last_error="; ".join(result.errors),
        )
        logger.info(
            "stock_profile_sync_completed symbol=%s status=%s changed=%s provider_symbol=%s",
            symbol,
            result.status.value,
            changed,
            result.profile.provider_symbol,
        )
        return result.status

    def _refresh_earnings(self, symbol: str) -> None:
        logger.info("earnings_sync_started symbol=%s", symbol)
        checked_at = self._now()
        old_events = load_earnings_events(self.engine, symbol)
        old_expected = next(
            (
                event
                for event in old_events
                if event.event_status.value == "EXPECTED"
            ),
            None,
        )
        try:
            result = self.earnings_provider.fetch_earnings(symbol)
        except Exception as exc:  # replaceable provider boundary
            logger.warning("earnings_sync_failed symbol=%s error=%s", symbol, exc)
            record_fundamental_sync_state(
                self.engine,
                symbol=symbol,
                dataset=EARNINGS_DATASET,
                status=ProfileStatus.UNAVAILABLE,
                source="yahoo",
                checked_at=checked_at,
                successful_at=None,
                payload_fingerprint=None,
                last_error=str(exc),
            )
            return
        if not isinstance(result, EarningsProviderResult):
            raise TypeError("earnings provider returned an invalid result")
        changed = 0
        if result.status is not ProfileStatus.UNAVAILABLE:
            changed = upsert_earnings_events(
                self.engine,
                result.events,
                replace_expected=True,
                symbol=symbol,
            )
        payload = []
        for event in result.events:
            item = asdict(event)
            for key in ("source_updated_at", "created_at", "updated_at"):
                item.pop(key, None)
            payload.append(item)
        fingerprint = normalized_payload_fingerprint(payload)
        successful_at = (
            result.checked_at
            if result.status is not ProfileStatus.UNAVAILABLE
            else None
        )
        record_fundamental_sync_state(
            self.engine,
            symbol=symbol,
            dataset=EARNINGS_DATASET,
            status=result.status,
            source=result.source,
            checked_at=result.checked_at,
            successful_at=successful_at,
            payload_fingerprint=fingerprint,
            last_error="; ".join(result.errors),
        )
        new_expected = next(
            (
                event
                for event in result.events
                if event.event_status.value == "EXPECTED"
            ),
            None,
        )
        if (
            old_expected is not None
            and new_expected is not None
            and old_expected.report_date != new_expected.report_date
        ):
            logger.info(
                "earnings_upcoming_date_changed symbol=%s old_date=%s new_date=%s",
                symbol,
                old_expected.report_date,
                new_expected.report_date,
            )
        logger.info(
            "earnings_sync_completed symbol=%s status=%s records=%d changed=%d",
            symbol,
            result.status.value,
            len(result.events),
            changed,
        )


def refresh_universe_stock_profiles(
    engine: Engine,
    symbols: Iterable[str],
    *,
    max_symbols: Optional[int] = 500,
    as_of: Optional[dt.datetime] = None,
    progress_callback: Optional[Callable[[str, int, int, int, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    service: Optional[ChartFundamentalService] = None,
) -> dict[str, int]:
    """Enrich a bounded stale slice while rotating past negative-cache rows."""

    now = _aware_utc(as_of or _utcnow())
    assert now is not None
    canonical_symbols = list(
        dict.fromkeys(
            canonical_symbol(symbol)
            for symbol in symbols
            if canonical_symbol(symbol)
        )
    )
    states = load_fundamental_sync_states(engine, PROFILE_DATASET)
    due = [
        symbol
        for symbol in canonical_symbols
        if ChartFundamentalService._state_due(
            states.get(symbol),
            now,
            normal_interval=PROFILE_FRESH_FOR,
            unavailable_interval=BULK_UNAVAILABLE_FRESH_FOR,
        )
    ]
    limit = None if max_symbols is None else max(0, int(max_symbols))
    selected = due if limit is None else due[:limit]
    worker = service or ChartFundamentalService(engine)
    refreshed = 0
    unavailable = 0
    total = len(selected)
    started = dt.datetime.now(dt.timezone.utc)
    for index, symbol in enumerate(selected, start=1):
        status = worker.refresh_profile(symbol)
        if status is ProfileStatus.UNAVAILABLE:
            unavailable += 1
        else:
            refreshed += 1
        if progress_callback is not None:
            elapsed = max(
                0.001,
                (dt.datetime.now(dt.timezone.utc) - started).total_seconds(),
            )
            remaining = max(0.0, (elapsed / index) * (total - index))
            progress_callback(
                symbol,
                index,
                total,
                int(index * 100 / total) if total else 100,
                f"{int(remaining // 60)}m {int(remaining % 60)}s",
            )
    summary = {
        "universe": len(canonical_symbols),
        "due": len(due),
        "attempted": total,
        "refreshed": refreshed,
        "unavailable": unavailable,
        "remaining": max(0, len(due) - total),
    }
    if log_callback is not None:
        log_callback(
            "Stock profile enrichment: "
            f"{refreshed} refreshed, {unavailable} unavailable, "
            f"{summary['remaining']} stale remaining."
        )
    return summary
