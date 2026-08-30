"""Daily precomputation and read-only chart lookup for market alignment."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

import pandas as pd
from sqlalchemy.engine import Engine

from src.core.market_alignment import (
    ALIGNMENT_BENCHMARK,
    DEFAULT_ALIGNMENT_CONFIG,
    AlignmentConfig,
    ContextResult,
    ContextState,
    DailySeriesMetrics,
    MarketAlignmentSnapshot,
    assign_market_segment,
    calculate_fallback_market_rs,
    calculate_leadership_score,
    calculate_overall_context,
    daily_series_metrics,
    deterministic_percentile,
    evaluate_industry_context,
    evaluate_market_context,
    evaluate_sector_context,
    evaluate_segment_context,
    finite_number,
    industry_peer_rankings,
    leadership_label,
)
from src.core.market_pulse import (
    INDUSTRIES_THEMES,
    SECTORS,
    load_market_pulse_instruments,
)
from src.infrastructure.database.repositories.fundamentals import (
    load_stock_profiles,
)
from src.infrastructure.database.repositories.market_alignment import (
    MarketAlignmentRepository,
)
from src.infrastructure.database.repositories.market_bars import (
    load_universe_history_from_db,
)
from src.infrastructure.database.repositories.scanner import (
    load_scanner_metrics_from_db,
    scanner_metrics_snapshot_date,
)
from src.utils.config import ROOT_DIR
from src.utils.market_calendar import expected_latest_market_data_date

logger = logging.getLogger(__name__)
DEFAULT_MARKET_PULSE_CONFIG_PATH = (
    ROOT_DIR / "config" / "market_pulse_instruments.json"
)

_SECTOR_ALIASES = {
    "basicmaterials": "materials",
    "communicationservices": "communicationservices",
    "communications": "communicationservices",
    "consumercyclical": "consumerdiscretionary",
    "consumerdiscretionary": "consumerdiscretionary",
    "financialservices": "financials",
    "finance": "financials",
    "healthcare": "healthcare",
    "industrial": "industrials",
    "telecommunications": "communicationservices",
    "technology": "technology",
}


def alignment_reference_tickers(
    config_path: Path = DEFAULT_MARKET_PULSE_CONFIG_PATH,
) -> tuple[str, ...]:
    """Daily proxy inputs downloaded by the normal universe batch, not the chart."""

    try:
        instruments = load_market_pulse_instruments(Path(config_path))
    except (OSError, ValueError, TypeError):
        instruments = ()
    values = [ALIGNMENT_BENCHMARK]
    values.extend(item.ticker for item in instruments if item.is_active)
    return tuple(dict.fromkeys(str(value).strip().upper() for value in values))


class MarketAlignmentService:
    """Single-query chart service with no migration, calculation, or provider path."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get_latest_market_alignment(
        self,
        symbol: str,
        *,
        expected_date: Optional[dt.date] = None,
    ) -> Optional[MarketAlignmentSnapshot]:
        started = time.perf_counter()
        canonical = str(symbol or "").strip().upper()
        try:
            snapshot = MarketAlignmentRepository(
                self.engine
            ).get_latest_market_alignment(canonical)
        except Exception as exc:
            if _is_missing_alignment_schema(exc):
                # A fresh installation may open the dashboard before the
                # daily schema/bootstrap job has run. Treat the optional
                # snapshot as unavailable; chart selection must not migrate.
                logger.debug(
                    "market_alignment_lookup schema=unavailable symbol=%s",
                    canonical,
                )
                return None
            logger.warning(
                "market_alignment_lookup_failed symbol=%s", canonical, exc_info=True
            )
            return None
        duration_ms = (time.perf_counter() - started) * 1000.0
        if snapshot is None:
            logger.debug(
                "market_alignment_lookup symbol=%s cache=miss duration_ms=%.3f",
                canonical,
                duration_ms,
            )
            return None
        latest_expected = expected_date or expected_latest_market_data_date()
        result = snapshot.with_stale(snapshot.as_of_date < latest_expected)
        logger.debug(
            "market_alignment_lookup symbol=%s cache=hit as_of=%s stale=%s duration_ms=%.3f",
            canonical,
            result.as_of_date,
            result.is_stale,
            duration_ms,
        )
        return result


def _is_missing_alignment_schema(exc: Exception) -> bool:
    """Recognize an unprovisioned optional snapshot table without another query."""

    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ()) or getattr(exc, "args", ())
    if args and args[0] == 1146:  # MySQL: table does not exist.
        return True
    message = str(original or exc).lower()
    mentions_alignment = any(
        table in message
        for table in ("stock_market_alignment_daily", "market_alignment_batches")
    )
    return mentions_alignment and (
        "doesn't exist" in message
        or "does not exist" in message
        or "no such table" in message
        or "undefined table" in message
    )


def refresh_market_alignment_to_db(
    stock_symbols: Sequence[str],
    engine: Engine,
    *,
    as_of_date: Optional[dt.date] = None,
    force: bool = False,
    config: AlignmentConfig = DEFAULT_ALIGNMENT_CONFIG,
    market_pulse_config_path: Path = DEFAULT_MARKET_PULSE_CONFIG_PATH,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Mapping[str, object]:
    """Calculate every symbol in batches and atomically publish one EOD date.

    This function uses only local SQL data. It never invokes yfinance, KIS,
    Market Pulse refresh, or a per-symbol metadata provider.
    """

    started = time.perf_counter()
    symbols = tuple(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in stock_symbols
            if str(symbol).strip().upper() != config.benchmark
        )
    )
    if not symbols:
        raise ValueError("Market alignment requires at least one stock symbol")
    completed_date = as_of_date or expected_latest_market_data_date()
    if isinstance(completed_date, dt.datetime):
        completed_date = completed_date.date()

    repository = MarketAlignmentRepository(engine)
    if not force and repository.is_batch_published(
        completed_date, config.feature_version
    ):
        summary = {
            "as_of_date": completed_date.isoformat(),
            "feature_version": config.feature_version,
            "skipped": True,
            "symbols_calculated": 0,
        }
        if log_callback:
            log_callback(
                f"Leadership/context snapshot already current for {completed_date} -- skipping."
            )
        return summary

    if log_callback:
        log_callback(
            f"Pre-calculating Leadership and Market Context for {len(symbols)} symbols..."
        )

    instruments = tuple(
        item
        for item in load_market_pulse_instruments(Path(market_pulse_config_path))
        if item.is_active
    )
    proxy_tickers = tuple(dict.fromkeys(item.ticker for item in instruments))
    history_symbols = tuple(
        dict.fromkeys((config.benchmark, *symbols, *proxy_tickers))
    )
    histories = load_universe_history_from_db(
        list(history_symbols),
        engine,
        start=dt.datetime.combine(
            completed_date - dt.timedelta(days=550), dt.time.min
        ),
        end=dt.datetime.combine(completed_date, dt.time.max),
        interval="1d",
    )
    benchmark_metrics = daily_series_metrics(
        histories.get(config.benchmark, pd.DataFrame()), completed_date
    )
    if benchmark_metrics.source_date != completed_date or benchmark_metrics.close is None:
        raise RuntimeError(
            f"{config.benchmark} completed daily data is unavailable for {completed_date}"
        )

    snapshot_date = scanner_metrics_snapshot_date(completed_date)
    scanner_rows = load_scanner_metrics_from_db(
        list(symbols), engine, date=snapshot_date
    )
    metrics_by_symbol = {
        str(item.get("symbol") or "").strip().upper(): item
        for item in scanner_rows
        if item.get("symbol")
    }
    eligible = tuple(
        symbol
        for symbol in symbols
        if _eligible_for_alignment(
            metrics_by_symbol.get(symbol),
            histories.get(symbol),
            completed_date,
            config,
        )
    )

    primary_market_rs = {
        symbol: value
        for symbol in eligible
        if (
            value := finite_number(
                (metrics_by_symbol.get(symbol) or {}).get(
                    config.market_rs_source_field
                )
            )
        )
        is not None
    }
    missing_primary = [symbol for symbol in eligible if symbol not in primary_market_rs]
    fallback_universe_rs, fallback_universe_components = calculate_fallback_market_rs(
        histories, eligible, completed_date
    )
    fallback_rs = {
        symbol: fallback_universe_rs[symbol]
        for symbol in missing_primary
        if symbol in fallback_universe_rs
    }
    fallback_components = {
        symbol: fallback_universe_components[symbol]
        for symbol in missing_primary
        if symbol in fallback_universe_components
    }
    market_rs = dict(primary_market_rs)
    market_rs.update(fallback_rs)

    profiles = load_stock_profiles(engine)
    classifications = {
        symbol: _classification_for_profile(profiles.get(symbol))
        for symbol in symbols
    }
    peer_results = industry_peer_rankings(
        market_rs,
        classifications,
        minimum_peer_count=config.minimum_peer_count,
    )

    proxy_metrics = {
        ticker: daily_series_metrics(histories.get(ticker, pd.DataFrame()), completed_date)
        for ticker in proxy_tickers
    }
    sector_proxies, industry_proxies = _proxy_maps(instruments)
    sector_percentiles = _proxy_percentiles(
        sector_proxies, proxy_metrics, "return_20d"
    )
    industry_baskets = _build_industry_baskets(
        histories,
        classifications,
        eligible,
        completed_date,
        config,
    )
    industry_universe_returns = {
        f"proxy:{group_key}": metrics.return_20d
        for group_key, ticker in industry_proxies.items()
        if (metrics := proxy_metrics.get(ticker)) is not None
        and metrics.return_20d is not None
    }
    industry_universe_returns.update(
        {
            f"basket:{group_key}": payload["metrics"].return_20d
            for group_key, payload in industry_baskets.items()
            if group_key not in industry_proxies
            and payload["metrics"].return_20d is not None
        }
    )
    industry_percentiles = deterministic_percentile(industry_universe_returns)

    market_context = evaluate_market_context(benchmark_metrics)
    calculated_at = dt.datetime.now(dt.timezone.utc)
    rows = []
    classification_misses = 0
    industry_fallbacks = 0
    unknown_components = 0
    sector_peer_fallbacks = 0

    for symbol in symbols:
        profile = profiles.get(symbol)
        classification = classifications[symbol]
        market_cap = finite_number(getattr(profile, "market_cap", None))
        market_cap_date = getattr(profile, "market_cap_as_of_date", None)
        segment_name, segment_proxy = assign_market_segment(market_cap, config)

        sector_name = _optional_text(classification.get("sector_name"))
        sector_key = _normalized_group(sector_name)
        resolved_sector_key = _SECTOR_ALIASES.get(sector_key, sector_key)
        sector_proxy = sector_proxies.get(resolved_sector_key)
        sector_metrics = proxy_metrics.get(sector_proxy, DailySeriesMetrics())
        sector_percentile = sector_percentiles.get(resolved_sector_key.upper())

        industry_name = _optional_text(classification.get("industry_name"))
        industry_key = _normalized_group(
            classification.get("industry_id") or industry_name
        )
        configured_industry_proxy = industry_proxies.get(industry_key)
        if configured_industry_proxy:
            industry_metrics = proxy_metrics.get(
                configured_industry_proxy, DailySeriesMetrics()
            )
            industry_source = configured_industry_proxy
            industry_percentile = industry_percentiles.get(
                f"proxy:{industry_key}".upper()
            )
            industry_meta = {
                "source": "configured_proxy",
                "constituent_count": None,
                "coverage": None,
            }
        else:
            basket = industry_baskets.get(industry_key)
            industry_metrics = (
                basket["metrics"] if basket is not None else DailySeriesMetrics()
            )
            industry_source = (
                f"Internal equal-weight basket ({basket['constituent_count']})"
                if basket is not None
                else None
            )
            industry_percentile = industry_percentiles.get(
                f"basket:{industry_key}".upper()
            )
            industry_meta = {
                "source": "internal_equal_weight_basket" if basket else "unavailable",
                "constituent_count": basket["constituent_count"] if basket else None,
                "coverage": basket["coverage"] if basket else None,
            }
            if basket is not None:
                industry_fallbacks += 1

        segment_metrics = proxy_metrics.get(segment_proxy, DailySeriesMetrics())
        segment_context = (
            evaluate_segment_context(segment_metrics, benchmark_metrics.return_5d)
            if segment_name and segment_proxy
            else _unknown_context()
        )
        sector_context = (
            evaluate_sector_context(
                sector_metrics,
                benchmark_metrics.return_5d,
                sector_percentile,
            )
            if sector_name and sector_proxy
            else _unknown_context()
        )
        industry_context = (
            evaluate_industry_context(
                industry_metrics,
                sector_metrics.return_5d,
                industry_percentile,
            )
            if industry_name and industry_source
            else _unknown_context()
        )
        overall = calculate_overall_context(
            market_context.state,
            segment_context.state,
            sector_context.state,
            industry_context.state,
        )

        peer = peer_results.get(
            symbol,
            {
                "industry_peer_rs": None,
                "peer_basis": "unavailable",
                "peer_count": 0,
                "peer_group_id": industry_key or resolved_sector_key or None,
                "peer_group_name": industry_name or sector_name,
            },
        )
        own_market_rs = market_rs.get(symbol)
        peer_rs = peer.get("industry_peer_rs")
        leadership_score = calculate_leadership_score(own_market_rs, peer_rs)
        market_rs_source = (
            "scanner_growth_rank_1m"
            if symbol in primary_market_rs
            else "fallback_63_126_252"
            if symbol in fallback_rs
            else "unavailable"
        )
        details = _calculation_details(
            completed_date=completed_date,
            calculated_at=calculated_at,
            config=config,
            market_rs=own_market_rs,
            market_rs_source=market_rs_source,
            market_rs_components=fallback_components.get(symbol),
            peer=peer,
            leadership_score=leadership_score,
            benchmark_metrics=benchmark_metrics,
            market_context=market_context,
            segment_name=segment_name,
            segment_proxy=segment_proxy,
            segment_metrics=segment_metrics,
            segment_context=segment_context,
            sector_name=sector_name,
            sector_proxy=sector_proxy,
            sector_metrics=sector_metrics,
            sector_percentile=sector_percentile,
            sector_context=sector_context,
            industry_name=industry_name,
            industry_source=industry_source,
            industry_metrics=industry_metrics,
            industry_percentile=industry_percentile,
            industry_context=industry_context,
            industry_meta=industry_meta,
            overall=overall,
            profile=profile,
            market_cap=market_cap,
            market_cap_date=market_cap_date,
        )
        classification_source = str(getattr(profile, "source", None) or "unavailable")
        row = MarketAlignmentSnapshot(
            symbol=symbol,
            as_of_date=completed_date,
            feature_version=config.feature_version,
            market_rs=own_market_rs,
            market_rs_source=market_rs_source,
            industry_peer_rs=peer_rs,
            peer_basis=str(peer.get("peer_basis") or "unavailable"),
            peer_count=int(peer.get("peer_count") or 0),
            peer_group_id=_optional_text(peer.get("peer_group_id")),
            peer_group_name=_optional_text(peer.get("peer_group_name")),
            leadership_score=leadership_score,
            leadership_label=leadership_label(leadership_score),
            market_state=market_context.state,
            market_conditions_passed=market_context.conditions_passed,
            market_cap=market_cap,
            market_cap_as_of_date=market_cap_date,
            segment_name=segment_name,
            segment_proxy=segment_proxy,
            segment_state=segment_context.state,
            segment_conditions_passed=segment_context.conditions_passed,
            sector_name=sector_name,
            sector_proxy=sector_proxy,
            sector_state=sector_context.state,
            sector_conditions_passed=sector_context.conditions_passed,
            industry_name=industry_name,
            industry_proxy_or_index=industry_source,
            industry_state=industry_context.state,
            industry_conditions_passed=industry_context.conditions_passed,
            context_points=overall.points,
            context_available_components=overall.available_components,
            context_label=overall.label,
            is_provisional=overall.is_provisional,
            classification_source=classification_source,
            calculated_at=calculated_at,
            calculation_details=details,
        )
        rows.append(row)
        if not sector_name or not industry_name:
            classification_misses += 1
        if row.peer_basis == "sector_fallback":
            sector_peer_fallbacks += 1
        unknown_components += sum(
            state is ContextState.UNKNOWN
            for state in (
                row.market_state,
                row.segment_state,
                row.sector_state,
                row.industry_state,
            )
        )

    stats = {
        "symbols_calculated": len(rows),
        "eligible_symbols": len(eligible),
        "classification_misses": classification_misses,
        "sector_peer_fallbacks": sector_peer_fallbacks,
        "industry_basket_fallbacks": industry_fallbacks,
        "unknown_components": unknown_components,
        "duration_seconds": round(time.perf_counter() - started, 3),
    }
    fingerprint = _input_fingerprint(
        completed_date,
        config.feature_version,
        history_symbols,
        histories,
        profiles,
    )
    published = repository.publish_batch(
        rows,
        input_fingerprint=fingerprint,
        stats=stats,
    )
    summary = {
        "as_of_date": completed_date.isoformat(),
        "feature_version": config.feature_version,
        "skipped": False,
        **stats,
        "published_rows": published,
    }
    logger.info("market_alignment_batch_published", extra=summary)
    if log_callback:
        log_callback(
            "Leadership/context snapshot published: "
            f"{published} symbols, {classification_misses} classification misses, "
            f"{sector_peer_fallbacks} sector peer fallbacks, "
            f"{unknown_components} unknown components, "
            f"{stats['duration_seconds']:.3f}s."
        )
    return summary


def _classification_for_profile(profile) -> dict[str, object]:
    if profile is None:
        return {
            "sector_id": "",
            "sector_name": None,
            "industry_id": "",
            "industry_name": None,
        }
    sector_name = _optional_text(getattr(profile, "sector_name", None))
    industry_name = _optional_text(getattr(profile, "industry_name", None))
    return {
        "sector_id": _normalized_group(
            getattr(profile, "sector_key", None) or sector_name
        ),
        "sector_name": sector_name,
        "industry_id": _normalized_group(
            getattr(profile, "industry_key", None) or industry_name
        ),
        "industry_name": industry_name,
    }


def _eligible_for_alignment(
    metrics: Optional[Mapping[str, object]],
    history: Optional[pd.DataFrame],
    as_of_date: dt.date,
    config: AlignmentConfig,
) -> bool:
    if metrics:
        volume = finite_number(metrics.get("volume"))
        dollar_volume = finite_number(metrics.get("dollar_volume"))
        history_days = finite_number(metrics.get("price_history_days"))
        return bool(
            volume is not None
            and dollar_volume is not None
            and history_days is not None
            and volume >= config.minimum_volume
            and dollar_volume >= config.minimum_dollar_volume
            and history_days >= 21
        )
    if history is None or history.empty:
        return False
    frame = history.copy()
    try:
        index = pd.DatetimeIndex(pd.to_datetime(frame.index))
        if index.tz is not None:
            index = index.tz_localize(None)
        frame.index = index
        frame = frame.loc[frame.index.date <= as_of_date].sort_index()
        if frame.empty or frame.index[-1].date() != as_of_date:
            return False
        close = finite_number(frame["Close"].iloc[-1])
        volume = finite_number(frame["Volume"].iloc[-1])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        len(frame) >= 21
        and close is not None
        and volume is not None
        and volume >= config.minimum_volume
        and close * volume >= config.minimum_dollar_volume
    )


def _proxy_maps(instruments) -> tuple[dict[str, str], dict[str, str]]:
    sector_proxies = {}
    industry_proxies = {}
    for item in instruments:
        key = _normalized_group(item.display_name)
        if item.section == SECTORS:
            sector_proxies.setdefault(key, item.ticker)
        elif item.section == INDUSTRIES_THEMES:
            industry_proxies.setdefault(key, item.ticker)
    return sector_proxies, industry_proxies


def _proxy_percentiles(
    proxy_map: Mapping[str, str],
    metrics: Mapping[str, DailySeriesMetrics],
    field_name: str,
) -> dict[str, float]:
    values = {
        group_key: getattr(metrics.get(ticker, DailySeriesMetrics()), field_name)
        for group_key, ticker in proxy_map.items()
    }
    return deterministic_percentile(values)


def _build_industry_baskets(
    histories: Mapping[str, pd.DataFrame],
    classifications: Mapping[str, Mapping[str, object]],
    eligible_symbols: Sequence[str],
    as_of_date: dt.date,
    config: AlignmentConfig,
) -> dict[str, dict[str, object]]:
    members: dict[str, list[str]] = defaultdict(list)
    for symbol in eligible_symbols:
        industry = _normalized_group(
            classifications.get(symbol, {}).get("industry_id")
        )
        if industry:
            members[industry].append(symbol)

    result = {}
    for industry, symbols in members.items():
        if len(symbols) < config.minimum_industry_constituents:
            continue
        series = {}
        for symbol in symbols:
            frame = histories.get(symbol)
            if frame is None or frame.empty or "Close" not in frame.columns:
                continue
            normalized = frame.copy()
            index = pd.DatetimeIndex(pd.to_datetime(normalized.index))
            if index.tz is not None:
                index = index.tz_localize(None)
            normalized.index = pd.DatetimeIndex(
                [pd.Timestamp(value.date()) for value in index]
            )
            normalized = normalized.loc[normalized.index.date <= as_of_date]
            column = "Adj Close" if "Adj Close" in normalized.columns else "Close"
            values = pd.to_numeric(normalized[column], errors="coerce")
            if column == "Adj Close" and values.iloc[-253:].isna().any():
                values = pd.to_numeric(normalized["Close"], errors="coerce")
            series[symbol] = values[~values.index.duplicated(keep="last")]
        if len(series) < config.minimum_industry_constituents:
            continue
        prices = pd.concat(series, axis=1).sort_index()
        returns = prices.pct_change(fill_method=None)
        required = max(
            config.minimum_industry_constituents,
            int(math.ceil(len(series) * config.industry_coverage_ratio)),
        )
        valid_count = returns.notna().sum(axis=1)
        basket_returns = returns.mean(axis=1, skipna=True).where(
            valid_count >= required
        )
        if pd.Timestamp(as_of_date) not in basket_returns.index:
            continue
        recent = basket_returns.iloc[-21:]
        if len(recent) < 21 or recent.isna().any():
            continue
        missing_positions = [
            position
            for position, missing in enumerate(basket_returns.isna())
            if bool(missing)
        ]
        contiguous_start = (missing_positions[-1] + 1) if missing_positions else 0
        basket_returns = basket_returns.iloc[contiguous_start:]
        if len(basket_returns) < 21:
            continue
        # Start a fresh normalized index after the latest insufficient-coverage
        # session. An unavailable group return is never converted to zero.
        index_values = (1.0 + basket_returns).cumprod() * 100.0
        basket_frame = pd.DataFrame(
            {"Close": index_values, "Adj Close": index_values},
            index=index_values.index,
        )
        metrics = daily_series_metrics(basket_frame, as_of_date)
        result[industry] = {
            "metrics": metrics,
            "constituent_count": len(series),
            "coverage": float(valid_count.loc[pd.Timestamp(as_of_date)] / len(series)),
        }
    return result


def _unknown_context() -> ContextResult:
    return ContextResult(ContextState.UNKNOWN, (None, None, None))


def _metric_payload(metrics: DailySeriesMetrics) -> dict[str, object]:
    return {
        "close": metrics.close,
        "sma20": metrics.sma20,
        "sma50": metrics.sma50,
        "return_5d": metrics.return_5d,
        "return_20d": metrics.return_20d,
        "source_date": metrics.source_date,
    }


def _condition_payload(names: Sequence[str], result: ContextResult) -> list[dict]:
    return [
        {"name": name, "result": value}
        for name, value in zip(names, result.conditions)
    ]


def _calculation_details(**values) -> dict[str, object]:
    config: AlignmentConfig = values["config"]
    overall = values["overall"]
    peer = values["peer"]
    profile = values["profile"]
    return {
        "leadership": {
            "formula": "0.60 × Market RS + 0.40 × Industry Peer RS",
            "market_rs": values["market_rs"],
            "market_rs_source": values["market_rs_source"],
            "market_rs_fallback_components": values["market_rs_components"],
            "industry_peer_rs": peer.get("industry_peer_rs"),
            "peer_group": peer.get("peer_group_name"),
            "peer_group_id": peer.get("peer_group_id"),
            "peer_count": peer.get("peer_count"),
            "peer_basis": peer.get("peer_basis"),
            "leadership_score": values["leadership_score"],
            "weights": {"market_rs": 0.60, "industry_peer_rs": 0.40},
        },
        "market": {
            "benchmark": config.benchmark,
            **_metric_payload(values["benchmark_metrics"]),
            "conditions": _condition_payload(
                ("close_above_sma20", "close_above_sma50", "return_5d_positive"),
                values["market_context"],
            ),
            "state": values["market_context"].state.value,
        },
        "segment": {
            "name": values["segment_name"],
            "proxy": values["segment_proxy"],
            **_metric_payload(values["segment_metrics"]),
            "spy_return_5d": values["benchmark_metrics"].return_5d,
            "conditions": _condition_payload(
                ("close_above_sma20", "return_5d_positive", "outperforming_spy_5d"),
                values["segment_context"],
            ),
            "state": values["segment_context"].state.value,
            "market_cap": values["market_cap"],
            "market_cap_as_of_date": values["market_cap_date"],
        },
        "sector": {
            "name": values["sector_name"],
            "proxy": values["sector_proxy"],
            **_metric_payload(values["sector_metrics"]),
            "spy_return_5d": values["benchmark_metrics"].return_5d,
            "performance_percentile_20d": values["sector_percentile"],
            "conditions": _condition_payload(
                (
                    "close_above_sma20",
                    "outperforming_spy_5d",
                    "performance_percentile_20d_at_least_70",
                ),
                values["sector_context"],
            ),
            "state": values["sector_context"].state.value,
        },
        "industry": {
            "name": values["industry_name"],
            "proxy_or_index": values["industry_source"],
            **_metric_payload(values["industry_metrics"]),
            "sector_return_5d": values["sector_metrics"].return_5d,
            "performance_percentile_20d": values["industry_percentile"],
            "conditions": _condition_payload(
                (
                    "close_above_sma20",
                    "outperforming_sector_5d",
                    "performance_percentile_20d_at_least_70",
                ),
                values["industry_context"],
            ),
            "state": values["industry_context"].state.value,
            **values["industry_meta"],
        },
        "metadata": {
            "as_of_date": values["completed_date"],
            "calculated_at": values["calculated_at"],
            "feature_version": config.feature_version,
            "classification_source": str(
                getattr(profile, "source", None) or "unavailable"
            ),
            "data_status": "provisional" if overall.is_provisional else "complete",
            "available_components": overall.available_components,
            "context_points": overall.points,
            "normalized_context_points": overall.normalized_points,
            "context_label": overall.label,
            "themes": [],
        },
    }


def _input_fingerprint(
    as_of_date: dt.date,
    version: str,
    symbols: Sequence[str],
    histories: Mapping[str, pd.DataFrame],
    profiles: Mapping[str, object],
) -> str:
    values = [f"date={as_of_date.isoformat()}", f"version={version}"]
    for symbol in sorted(symbols):
        history = histories.get(symbol)
        if history is None or history.empty:
            latest = ""
            count = 0
        else:
            latest = pd.Timestamp(history.index[-1]).date().isoformat()
            count = len(history)
        profile = profiles.get(symbol)
        revision = str(getattr(profile, "updated_at", ""))
        values.append(f"{symbol}|{latest}|{count}|{revision}")
    return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()


def _normalized_group(value: object) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def _optional_text(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None
