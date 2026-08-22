"""Scanner-metric persistence and derived-cache refresh."""

import datetime as dt
import hashlib
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import (Boolean, Float, Integer, MetaData, Numeric, String,
                        Table, and_, case, cast, delete, false, func, select)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from src.utils.market_calendar import expected_latest_market_data_date

from ..formatting import _format_elapsed, _format_eta
from ..schema import (_ensure_scanner_metric_snapshots_table,
                      _ensure_scanner_metrics_table,
                      _get_scanner_metrics_table,
                      _get_stock_profiles_table)
from ..settings import (REFERENCE_SYMBOL, SCANNER_METRIC_WRITE_CHUNK_SIZE,
                        SCANNER_METRICS_CACHE_VERSION,
                        SCANNER_QUERY_SYMBOL_CHUNK_SIZE)
from ..sql_helpers import _clean_symbols, _execute_bulk_upsert, _record_chunks
from ..time_utils import _utcnow_naive
from .market_bars import (load_symbol_history_from_db,
                          load_universe_history_from_db)
from .market_watermarks import get_price_history_watermarks


def scanner_metrics_snapshot_date(expected_date: Optional[dt.date] = None) -> dt.datetime:
    """Database key for metrics derived from the latest completed market session."""
    market_date = expected_date or expected_latest_market_data_date()
    if isinstance(market_date, dt.datetime):
        market_date = market_date.date()
    return dt.datetime.combine(market_date, dt.time.min)


def _scanner_metric_and_input_symbols(tickers: List[str]) -> Tuple[List[str], List[str]]:
    metric_symbols = [
        symbol for symbol in _clean_symbols(tickers) if symbol != REFERENCE_SYMBOL
    ]
    return metric_symbols, [REFERENCE_SYMBOL, *metric_symbols]


def scanner_metrics_input_fingerprint(
    tickers: List[str], history_watermarks: Dict[str, object]
) -> str:
    """Hash the calculation version, universe, and causal daily inputs."""
    entries = [f"version={SCANNER_METRICS_CACHE_VERSION}"]
    for symbol in sorted(_clean_symbols(tickers)):
        watermark = history_watermarks.get(symbol)
        if isinstance(watermark, dict):
            latest = watermark.get("latest_date")
            row_count = int(watermark.get("row_count") or 0)
        elif isinstance(watermark, (tuple, list)) and len(watermark) >= 2:
            latest, row_count = watermark[0], int(watermark[1] or 0)
        else:
            # Retain a tolerant public helper for callers/tests supplying the
            # former latest-date-only shape. New cache decisions always pass
            # the stronger (latest date, row count) watermark.
            latest, row_count = watermark, 0
        if latest is None or pd.isna(latest):
            latest_text = ""
        else:
            latest_text = pd.Timestamp(latest).date().isoformat()
        entries.append(f"{symbol}|{latest_text}|{row_count}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def is_scanner_metrics_snapshot_current(
    engine: Engine,
    tickers: List[str],
    history_watermarks: Optional[Dict[str, object]] = None,
    snapshot_date: Optional[dt.datetime] = None,
    strict: bool = False,
) -> bool:
    """Whether a complete scanner snapshot matches the current daily inputs."""
    metric_symbols, input_symbols = _scanner_metric_and_input_symbols(tickers)
    if not metric_symbols:
        return False
    if history_watermarks is None:
        history_watermarks = get_price_history_watermarks(
            engine, input_symbols, interval="1d", strict=strict
        )
    fingerprint = scanner_metrics_input_fingerprint(input_symbols, history_watermarks)
    snapshot_date = snapshot_date or scanner_metrics_snapshot_date()

    try:
        metrics_table = _ensure_scanner_metrics_table(engine)
        snapshots_table = _ensure_scanner_metric_snapshots_table(engine)
        snapshot_stmt = select(
            snapshots_table.c.input_fingerprint,
            snapshots_table.c.metric_count,
        ).where(snapshots_table.c.snapshot_date == snapshot_date)
        with engine.connect() as conn:
            snapshot = conn.execute(snapshot_stmt).one_or_none()
            if snapshot is None or snapshot.input_fingerprint != fingerprint:
                return False
            actual_count = 0
            for chunk in _record_chunks(
                metric_symbols, SCANNER_QUERY_SYMBOL_CHUNK_SIZE
            ):
                count_stmt = select(func.count()).select_from(metrics_table).where(
                    metrics_table.c.symbol.in_(chunk),
                    metrics_table.c.date == snapshot_date,
                )
                actual_count += int(conn.execute(count_stmt).scalar_one())
    except SQLAlchemyError as exc:
        if strict:
            raise RuntimeError("Unable to verify scanner-metrics snapshot") from exc
        return False
    return int(snapshot.metric_count) > 0 and actual_count == int(snapshot.metric_count)


def get_universe_stock_metrics_from_db(
    tickers: List[str],
    engine: Engine,
    min_history_days: int = 1,
    lookback_days: int = 380,
) -> List[dict]:
    metric_symbols, input_symbols = _scanner_metric_and_input_symbols(tickers)
    if not metric_symbols:
        return []
    snapshot_date = scanner_metrics_snapshot_date()
    history_watermarks = get_price_history_watermarks(
        engine, input_symbols, interval="1d", strict=True
    )
    if is_scanner_metrics_snapshot_current(
        engine,
        metric_symbols,
        history_watermarks=history_watermarks,
        snapshot_date=snapshot_date,
        strict=True,
    ):
        return load_scanner_metrics_from_db(metric_symbols, engine, snapshot_date)

    from src.utils.data_loader import compute_stock_metrics

    metrics = []
    start_date = _utcnow_naive() - dt.timedelta(days=lookback_days)
    histories = load_universe_history_from_db(input_symbols, engine, start=start_date)
    if not histories:
        return []

    spy_history = histories.get("SPY")
    if spy_history is None:
        try:
            spy_history = load_symbol_history_from_db("SPY", engine, interval="1d")
        except Exception:
            spy_history = None

    for symbol in metric_symbols:
        history = histories.get(symbol)
        if history is None or history.empty:
            continue
        result = compute_stock_metrics(symbol, history, min_history_days=min_history_days, spy_history=spy_history)
        if result is not None:
            metrics.append(result)

    if not metrics:
        return []

    # Rank 1-month growth (populated in 'growth_rank' and 'growth_rank_1m')
    growth_values_1m = [item.get("return_1m", 0.0) for item in metrics]
    ranks_1m = pd.Series(growth_values_1m).rank(pct=True, method="max") * 100
    for idx, item in enumerate(metrics):
        item["growth_rank"] = float(ranks_1m.iloc[idx])
        item["growth_rank_1m"] = float(ranks_1m.iloc[idx])

    # Rank 3-month growth
    growth_values_3m = [item.get("return_3m", 0.0) for item in metrics]
    ranks_3m = pd.Series(growth_values_3m).rank(pct=True, method="max") * 100
    for idx, item in enumerate(metrics):
        item["growth_rank_3m"] = float(ranks_3m.iloc[idx])

    return metrics


def save_scanner_metrics_to_db(symbol: str, metrics: dict, date: dt.datetime, engine: Engine) -> bool:
    """Save computed scanner metrics for a symbol to the database."""
    metadata = MetaData()
    table = _get_scanner_metrics_table(metadata)
    row_data = _scanner_metric_record(symbol, metrics, date, table)
            
    try:
        with engine.begin() as conn:
            _execute_bulk_upsert(
                conn,
                table,
                [row_data],
                ("symbol", "date"),
                engine.dialect.name,
            )
        return True
    except SQLAlchemyError:
        return False


def _scanner_metric_record(symbol: str, metrics: dict, date: dt.datetime, table: Table) -> dict:
    row_data = {
        "symbol": symbol.strip().upper(),
        "date": date,
        "updated_at": _utcnow_naive(),
    }
    for key, value in metrics.items():
        if key not in table.columns:
            continue
        if pd.isna(value):
            value = None
        elif isinstance(value, (np.int64, np.int32)):
            value = int(value)
        elif isinstance(value, (np.float64, np.float32)):
            value = float(value)
        elif isinstance(value, (np.bool_)):
            value = bool(value)
        row_data[key] = value
    return row_data


def save_scanner_metrics_batch_to_db(metrics_list: List[dict], date: dt.datetime, engine: Engine) -> List[str]:
    if not metrics_list:
        return []

    metadata = MetaData()
    table = _get_scanner_metrics_table(metadata)
    _ensure_scanner_metrics_table(engine)
    records = [
        _scanner_metric_record(item["symbol"], item, date, table)
        for item in metrics_list
        if item.get("symbol")
    ]
    if not records:
        return []

    try:
        with engine.begin() as conn:
            for chunk in _record_chunks(
                records, SCANNER_METRIC_WRITE_CHUNK_SIZE
            ):
                _execute_bulk_upsert(
                    conn,
                    table,
                    chunk,
                    ("symbol", "date"),
                    engine.dialect.name,
                )
        return [record["symbol"] for record in records]
    except SQLAlchemyError:
        return []


def save_scanner_metrics_snapshot_to_db(
    metrics_list: List[dict],
    snapshot_date: dt.datetime,
    input_fingerprint: str,
    engine: Engine,
    snapshot_symbols: Optional[List[str]] = None,
    strict: bool = False,
) -> List[str]:
    """Atomically replace a complete scanner snapshot in bounded statements."""
    if not metrics_list:
        return []
    try:
        metrics_table = _ensure_scanner_metrics_table(engine)
        snapshots_table = _ensure_scanner_metric_snapshots_table(engine)
        records = [
            _scanner_metric_record(item["symbol"], item, snapshot_date, metrics_table)
            for item in metrics_list
            if item.get("symbol")
        ]
        if not records:
            return []
        manifest = {
            "snapshot_date": snapshot_date,
            "input_fingerprint": input_fingerprint,
            "metric_count": len(records),
            "completed_at": _utcnow_naive(),
        }
        delete_symbols = _clean_symbols(
            snapshot_symbols
            or [record["symbol"] for record in records]
        )
        dialect_name = engine.dialect.name
        with engine.begin() as conn:
            # The primary key starts with symbol, so a date-only delete can
            # scan the entire historical metrics table and hit the socket
            # timeout. Keep every statement indexed and bounded while the
            # surrounding transaction preserves all-or-nothing replacement.
            for chunk in _record_chunks(
                delete_symbols, SCANNER_QUERY_SYMBOL_CHUNK_SIZE
            ):
                conn.execute(
                    delete(metrics_table).where(
                        metrics_table.c.symbol.in_(chunk),
                        metrics_table.c.date == snapshot_date,
                    )
                )
            for chunk in _record_chunks(
                records, SCANNER_METRIC_WRITE_CHUNK_SIZE
            ):
                _execute_bulk_upsert(
                    conn,
                    metrics_table,
                    chunk,
                    ("symbol", "date"),
                    dialect_name,
                )
            _execute_bulk_upsert(
                conn,
                snapshots_table,
                [manifest],
                ("snapshot_date",),
                dialect_name,
            )
    except SQLAlchemyError as exc:
        if strict:
            driver_error = getattr(exc, "orig", None)
            detail = f": {driver_error}" if driver_error is not None else ""
            raise RuntimeError(
                f"Unable to save scanner metrics snapshot{detail}"
            ) from exc
        return []
    return [record["symbol"] for record in records]


def load_scanner_metrics_from_db(tickers: List[str], engine: Engine, date: Optional[dt.datetime] = None) -> List[dict]:
    """Load cached scanner metrics from the active data engine."""
    if date is None:
        date = scanner_metrics_snapshot_date()
    symbols = _clean_symbols(tickers)
    if not symbols:
        return []

    metadata = MetaData()
    table = _get_scanner_metrics_table(metadata)

    try:
        with engine.connect() as conn:
            rows = []
            for chunk in _record_chunks(
                symbols, SCANNER_QUERY_SYMBOL_CHUNK_SIZE
            ):
                stmt = select(table).where(
                    table.c.symbol.in_(chunk), table.c.date == date
                )
                rows.extend(conn.execute(stmt).fetchall())

            results = []
            for row in rows:
                row_dict = {}
                for idx, col in enumerate(table.columns):
                    val = row[idx]
                    if isinstance(col.type, Boolean) and val is not None:
                        val = bool(val)
                    row_dict[col.name] = val
                results.append(row_dict)
            return results
    except SQLAlchemyError:
        return []


def _scanner_rule_expression(
    table: Table, rule: dict, column_overrides: Optional[dict] = None
):
    """Build one parameterized scanner predicate from a validated table column."""
    attribute = str(rule.get("attribute") or "").strip()
    overrides = column_overrides or {}
    if attribute in overrides:
        column = overrides[attribute]
    elif attribute in table.columns:
        column = table.c[attribute]
    else:
        return false()

    raw_threshold = rule.get("threshold", "")
    if isinstance(column.type, Boolean):
        if isinstance(raw_threshold, bool):
            threshold = raw_threshold
        else:
            value = str(raw_threshold).strip().lower()
            if value in ("true", "yes", "1"):
                threshold = True
            elif value in ("false", "no", "0"):
                threshold = False
            else:
                return false()
    elif isinstance(column.type, (Float, Integer, Numeric)):
        try:
            threshold = float(raw_threshold)
        except (TypeError, ValueError):
            return false()
    elif isinstance(column.type, String):
        threshold = str(raw_threshold)
    else:
        return false()

    operator = str(rule.get("operator") or ">=")
    comparison_column = column
    comparison_threshold = threshold
    if isinstance(column.type, Boolean) and operator in (">", "<", ">=", "<="):
        comparison_column = cast(column, Integer)
        comparison_threshold = int(threshold)

    if operator == ">":
        return comparison_column > comparison_threshold
    if operator == "<":
        return comparison_column < comparison_threshold
    if operator == "==":
        return comparison_column == comparison_threshold
    if operator == ">=":
        return comparison_column >= comparison_threshold
    if operator == "<=":
        return comparison_column <= comparison_threshold
    if operator == "!=":
        return comparison_column != comparison_threshold
    return false()


def _scanner_rows_to_dicts(table: Table, rows: list) -> List[dict]:
    results = []
    for row in rows:
        row_dict = {}
        for idx, column in enumerate(table.columns):
            value = row[idx]
            if isinstance(column.type, Boolean) and value is not None:
                value = bool(value)
            row_dict[column.name] = value
        results.append(row_dict)
    return results


def query_scanner_metrics_with_funnel(
    tickers: List[str],
    engine: Engine,
    rules: List[dict],
    date: Optional[dt.datetime] = None,
) -> Tuple[List[dict], dict]:
    """Filter the cached snapshot in SQL and count each cumulative rule stage."""
    symbols, _ = _scanner_metric_and_input_symbols(tickers)
    if not symbols:
        return [], {"universe_count": 0, "rule_counts": []}

    snapshot_date = date or scanner_metrics_snapshot_date()
    metadata = MetaData()
    table = _get_scanner_metrics_table(metadata)
    uses_name = any(
        str(rule.get("attribute") or "").strip() == "name" for rule in rules
    )
    profiles_table = _get_stock_profiles_table(metadata) if uses_name else None
    from_clause = (
        table.outerjoin(
            profiles_table, table.c.symbol == profiles_table.c.symbol
        )
        if profiles_table is not None
        else table
    )
    column_overrides = (
        {"name": profiles_table.c.company_name}
        if profiles_table is not None
        else {}
    )
    rule_expressions = [
        _scanner_rule_expression(table, rule, column_overrides)
        for rule in rules
    ]
    cumulative_expressions = []
    cumulative_expression = None
    for expression in rule_expressions:
        cumulative_expression = (
            and_(cumulative_expression, expression)
            if cumulative_expression is not None
            else expression
        )
        cumulative_expressions.append(cumulative_expression)

    universe_count = 0
    rule_counts = [0] * len(rule_expressions)
    rows = []
    query_chunk_size = (
        len(symbols)
        if getattr(engine.dialect, "name", "") == "mysql"
        else SCANNER_QUERY_SYMBOL_CHUNK_SIZE
    )
    try:
        with engine.connect() as conn:
            for chunk in _record_chunks(symbols, query_chunk_size):
                base_conditions = (
                    table.c.symbol.in_(chunk),
                    table.c.date == snapshot_date,
                    table.c.price_history_days >= 1,
                )
                aggregate_columns = [func.count().label("universe_count")]
                aggregate_columns.extend(
                    func.sum(case((expression, 1), else_=0)).label(
                        f"rule_count_{index}"
                    )
                    for index, expression in enumerate(cumulative_expressions)
                )
                aggregate_row = conn.execute(
                    select(*aggregate_columns).select_from(from_clause).where(
                        *base_conditions
                    )
                ).one()
                universe_count += int(aggregate_row[0] or 0)
                for index in range(len(rule_counts)):
                    rule_counts[index] += int(aggregate_row[index + 1] or 0)

                final_conditions = list(base_conditions)
                if cumulative_expressions:
                    final_conditions.append(cumulative_expressions[-1])
                result_columns = [table]
                if profiles_table is not None:
                    result_columns.append(
                        profiles_table.c.company_name.label("name")
                    )
                rows.extend(
                    conn.execute(
                        select(*result_columns)
                        .select_from(from_clause)
                        .where(*final_conditions)
                    ).fetchall()
                )
    except SQLAlchemyError as exc:
        driver_error = getattr(exc, "orig", None)
        detail = f": {driver_error}" if driver_error is not None else ""
        raise RuntimeError(f"Unable to query scanner metrics{detail}") from exc

    results = _scanner_rows_to_dicts(table, rows)
    if profiles_table is not None:
        name_index = len(table.columns)
        for result, row in zip(results, rows):
            result["name"] = row[name_index]

    return results, {
        "universe_count": universe_count,
        "rule_counts": rule_counts,
    }


def refresh_scanner_metrics_to_db(
    tickers: List[str],
    engine: Engine,
    log_callback: Optional[Callable[[str], None]] = None,
    force: bool = False,
    history_watermarks: Optional[Dict[str, object]] = None,
) -> List[str]:
    """Calculate and store scanner metrics for the universe."""
    symbols, input_symbols = _scanner_metric_and_input_symbols(tickers)
    if not symbols:
        return []
    snapshot_date = scanner_metrics_snapshot_date()
    if history_watermarks is None:
        history_watermarks = get_price_history_watermarks(
            engine, input_symbols, interval="1d", strict=True
        )
    input_fingerprint = scanner_metrics_input_fingerprint(
        input_symbols, history_watermarks
    )
    if not force and is_scanner_metrics_snapshot_current(
        engine,
        symbols,
        history_watermarks=history_watermarks,
        snapshot_date=snapshot_date,
        strict=True,
    ):
        if log_callback:
            log_callback(
                f"Scanner metrics already current for {snapshot_date.date()} -- skipping."
            )
        return []

    if log_callback:
        log_callback("Pre-calculating and saving scanner metrics...")

    try:
        # Compute directly so a stale snapshot is never read during generation.
        metrics_list = []
        start_date = _utcnow_naive() - dt.timedelta(days=380)
        histories = load_universe_history_from_db(input_symbols, engine, start=start_date)
        if not histories:
            if log_callback:
                log_callback("  Failed to load history for calculation.")
            return []

        spy_history = histories.get("SPY")
        if spy_history is None:
            try:
                spy_history = load_symbol_history_from_db("SPY", engine, interval="1d")
            except Exception:
                spy_history = None

        from src.utils.data_loader import compute_stock_metrics
        total_symbols = len(symbols)
        metric_start_ts = time.time()
        metric_progress_every = max(1, min(100, total_symbols // 20 or 1))
        if log_callback:
            log_callback(f"Calculating scanner metrics: 0/{total_symbols} (0%) - ETA calculating...")

        for index, symbol in enumerate(symbols, start=1):
            history = histories.get(symbol)
            if history is not None and not history.empty:
                result = compute_stock_metrics(symbol, history, spy_history=spy_history)
                if result is not None:
                    metrics_list.append(result)

            if log_callback and (index == total_symbols or index % metric_progress_every == 0):
                elapsed = time.time() - metric_start_ts
                avg_seconds = elapsed / max(1, index)
                eta_text = _format_eta(int(avg_seconds * max(0, total_symbols - index)))
                percent = int((index / total_symbols) * 100) if total_symbols else 100
                log_callback(
                    f"Scanner metrics progress: {index}/{total_symbols} ({percent}%) - "
                    f"calculated={len(metrics_list)}, ETA {eta_text}"
                )

        if not metrics_list:
            if log_callback:
                log_callback("  No metrics calculated.")
            return []

        # Ranks
        growth_values_1m = [item.get("return_1m", 0.0) for item in metrics_list]
        ranks_1m = pd.Series(growth_values_1m).rank(pct=True, method="max") * 100
        for idx, item in enumerate(metrics_list):
            item["growth_rank"] = float(ranks_1m.iloc[idx])
            item["growth_rank_1m"] = float(ranks_1m.iloc[idx])

        growth_values_3m = [item.get("return_3m", 0.0) for item in metrics_list]
        ranks_3m = pd.Series(growth_values_3m).rank(pct=True, method="max") * 100
        for idx, item in enumerate(metrics_list):
            item["growth_rank_3m"] = float(ranks_3m.iloc[idx])

        save_total = len(metrics_list)
        save_start_ts = time.time()
        if log_callback:
            log_callback(f"Saving scanner metrics: 0/{save_total} (0%) - ETA calculating...")

        saved = save_scanner_metrics_snapshot_to_db(
            metrics_list,
            snapshot_date,
            input_fingerprint,
            engine,
            snapshot_symbols=symbols,
            strict=True,
        )
        snapshot_complete = (
            len(saved) == len(metrics_list)
            and is_scanner_metrics_snapshot_current(
                engine,
                symbols,
                history_watermarks=history_watermarks,
                snapshot_date=snapshot_date,
                strict=True,
            )
        )
        if log_callback:
            elapsed = time.time() - save_start_ts
            log_callback(
                f"Scanner metrics save progress: {save_total}/{save_total} (100%) - "
                f"saved={len(saved)}, ETA 00:00, elapsed={_format_elapsed(elapsed)}"
            )
                
        if log_callback:
            if snapshot_complete:
                log_callback(
                    f"  Successfully saved scanner metrics for {len(saved)} symbols."
                )
            else:
                log_callback("  Failed to commit a complete scanner metrics snapshot.")
        return saved if snapshot_complete else []
    except Exception as exc:
        if log_callback:
            log_callback(f"  Failed to save scanner metrics: {exc}")
        return []
