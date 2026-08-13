"""Explicit bidirectional mirror reconciliation maintenance workflow."""

from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.engine import Engine

from .mirror_copy import (_copy_pc_partitions_to_local_exactly,
                          _insert_missing_local_raw_partitions,
                          _mismatched_partitions,
                          _scoped_partition_fingerprints,
                          _validate_reconcile_table_schema)
from .mirror_engine import (_RECONCILE_TABLE_SPECS, MIRRORED_TABLES,
                            LocalMirrorReconciliationResult,
                            _ensure_local_mirror_handoff_tracking,
                            _mark_local_mirror_handoff_clean,
                            _PartitionFingerprint)
from .repositories.chart_indicators import (get_chart_indicator_refresh_plan,
                                            refresh_chart_indicators_to_db)
from .repositories.market_watermarks import get_price_history_watermarks
from .repositories.scanner import (is_scanner_metrics_snapshot_current,
                                   refresh_scanner_metrics_to_db)
from .schema import (_ensure_price_history_table,
                     _ensure_symbol_refresh_failures_table)
from .settings import REFERENCE_SYMBOL
from .sql_helpers import _clean_symbols


def _reconciliation_universe(
    pc_engine: Engine,
    tickers: Optional[List[str]],
    affected_daily_symbols: Set[str],
) -> Tuple[List[str], List[str]]:
    """Return scanner and chart symbol sets without doing a network refresh."""
    scanner_symbols = _clean_symbols(tickers or [])
    if not scanner_symbols:
        try:
            from src.utils.data_loader import get_default_universe

            scanner_symbols = _clean_symbols(
                get_default_universe(max_symbols=None, refresh=False)
            )
        except Exception:
            scanner_symbols = []
    if not scanner_symbols:
        price_history = _ensure_price_history_table(pc_engine)
        with pc_engine.connect() as conn:
            scanner_symbols = _clean_symbols(
                list(
                    conn.execute(
                        select(price_history.c.symbol)
                        .where(price_history.c.interval == "1d")
                        .distinct()
                    ).scalars()
                )
            )

    scanner_symbols = list(
        dict.fromkeys([REFERENCE_SYMBOL, *scanner_symbols])
    )
    chart_symbols = list(
        dict.fromkeys(
            [REFERENCE_SYMBOL, *scanner_symbols, *sorted(affected_daily_symbols)]
        )
    )
    return scanner_symbols, chart_symbols


def _rebuild_and_verify_pc_derived_data(
    pc_engine: Engine,
    tickers: Optional[List[str]],
    affected_daily_symbols: Set[str],
) -> None:
    """Make PC chart/scanner caches causal to the reconciled daily history."""
    scanner_symbols, chart_symbols = _reconciliation_universe(
        pc_engine, tickers, affected_daily_symbols
    )
    chart_inputs = _clean_symbols(chart_symbols)
    history_watermarks = get_price_history_watermarks(
        pc_engine, chart_inputs, interval="1d", strict=True
    )

    chart_plan = get_chart_indicator_refresh_plan(
        pc_engine,
        chart_inputs,
        reference_symbol=REFERENCE_SYMBOL,
        history_watermarks=history_watermarks,
    )
    if chart_plan:
        refresh_chart_indicators_to_db(
            chart_inputs,
            pc_engine,
            reference_symbol=REFERENCE_SYMBOL,
            history_watermarks=history_watermarks,
            refresh_plan=chart_plan,
        )
        remaining = get_chart_indicator_refresh_plan(
            pc_engine,
            list(chart_plan),
            reference_symbol=REFERENCE_SYMBOL,
            history_watermarks=history_watermarks,
        )
        if remaining:
            raise RuntimeError(
                f"PC chart indicators remain incomplete for {len(remaining)} symbol(s)."
            )

    metric_symbols = [
        symbol for symbol in scanner_symbols if symbol != REFERENCE_SYMBOL
    ]
    if not metric_symbols:
        return
    metric_inputs = list(dict.fromkeys([REFERENCE_SYMBOL, *metric_symbols]))
    metric_watermarks = {
        symbol: history_watermarks.get(symbol)
        for symbol in metric_inputs
    }
    if any(value is None for value in metric_watermarks.values()):
        metric_watermarks = get_price_history_watermarks(
            pc_engine, metric_inputs, interval="1d", strict=True
        )
    if not is_scanner_metrics_snapshot_current(
        pc_engine,
        metric_symbols,
        history_watermarks=metric_watermarks,
        strict=True,
    ):
        refresh_scanner_metrics_to_db(
            metric_symbols,
            pc_engine,
            history_watermarks=metric_watermarks,
        )
    if not is_scanner_metrics_snapshot_current(
        pc_engine,
        metric_symbols,
        history_watermarks=metric_watermarks,
        strict=True,
    ):
        raise RuntimeError("PC scanner metrics remain incomplete after reconciliation.")


def reconcile_local_mirror_with_pc(
    pc_engine: Engine,
    local_engine: Engine,
    *,
    tickers: Optional[List[str]] = None,
    hourly_symbols: Optional[List[str]] = None,
    verify_derived: bool = True,
    tables: Tuple[Tuple[str, str], ...] = MIRRORED_TABLES,
) -> LocalMirrorReconciliationResult:
    """Converge market data before routing the dashboard from local to PC.

    The merge is intentionally asymmetric:

    * only missing, completed, validated raw daily/hourly bars may travel from
      local SQLite to PC MySQL;
    * those writes are insert-only, so an existing PC primary key always wins;
    * derived/operational tables never travel local -> PC;
    * PC-derived caches are rebuilt/verified, then the PC is mirrored back to
      the laptop and the raw per-symbol watermarks are verified both ways.

    Any error returns ``success=False``.  Callers must keep using the local
    engine and retry later rather than switching on a partial result.
    """
    requested_names = {table_name for table_name, _watermark in tables}
    reconcile_specs = [
        spec
        for spec in _RECONCILE_TABLE_SPECS
        if spec.table_name in requested_names
    ]
    unsupported = requested_names.difference(
        spec.table_name for spec in reconcile_specs
    )
    if unsupported:
        return LocalMirrorReconciliationResult(
            False,
            {},
            {},
            errors=(
                "No exact reconciliation policy for: "
                + ", ".join(sorted(unsupported)),
            ),
        )

    local_to_pc: Dict[str, int] = {}
    pc_to_local: Dict[str, int] = {}
    errors: List[str] = []
    affected_daily: Set[str] = set()
    affected_hourly: Set[str] = set()
    local_handoff_ready = False

    try:
        with pc_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        with local_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _ensure_symbol_refresh_failures_table(pc_engine)
        _ensure_symbol_refresh_failures_table(local_engine)
        _ensure_local_mirror_handoff_tracking(local_engine)
        # Never let two empty/equal legacy tables bypass safety checks merely
        # because there are no mismatched partitions for a copy helper to
        # inspect.  Every requested table must have the exact key shape before
        # any local row is eligible to reach the PC or the UI can switch green.
        for spec in reconcile_specs:
            pc_table = Table(
                spec.table_name, MetaData(), autoload_with=pc_engine
            )
            local_table = Table(
                spec.table_name, MetaData(), autoload_with=local_engine
            )
            _validate_reconcile_table_schema(pc_table, local_table, spec)
    except Exception as exc:
        return LocalMirrorReconciliationResult(
            False, {}, {}, errors=(f"Database readiness check failed: {exc}",)
        )

    # First find exact raw-partition differences and promote only composite
    # primary keys that do not exist on the PC.  Existing PC rows are never
    # updated, regardless of local timestamps or values.
    for spec in (item for item in reconcile_specs if item.reverse_raw):
        try:
            pc_fingerprints = _scoped_partition_fingerprints(
                pc_engine, spec, hourly_symbols=hourly_symbols
            )
            local_fingerprints = _scoped_partition_fingerprints(
                local_engine, spec, hourly_symbols=hourly_symbols
            )
            mismatched = _mismatched_partitions(
                pc_fingerprints, local_fingerprints
            )
            count, symbols = _insert_missing_local_raw_partitions(
                pc_engine,
                local_engine,
                spec,
                mismatched,
                pc_fingerprints,
                local_fingerprints,
            )
            local_to_pc[spec.table_name] = count
            if spec.table_name == "price_history":
                affected_daily.update(symbols)
            else:
                affected_hourly.update(symbols)
        except Exception as exc:
            errors.append(f"{spec.table_name} local -> PC failed: {exc}")

    # Always verify causal derived caches before a successful adoption.  Raw
    # inserts commit in retry-safe chunks; if a later table failed during a
    # previous pass, the next pass may see zero newly affected symbols even
    # though the PC's chart/scanner artifacts still need rebuilding.
    raw_pc_before_derived: Dict[
        str, Dict[Tuple[object, ...], _PartitionFingerprint]
    ] = {}
    if not errors and verify_derived:
        try:
            # This is the first side of a write fence.  If the PC refresh
            # changes raw history while derived caches are being rebuilt, the
            # canonical snapshot below will differ and this attempt must not
            # activate PC routing.
            for spec in (item for item in reconcile_specs if item.reverse_raw):
                raw_pc_before_derived[spec.table_name] = (
                    _scoped_partition_fingerprints(
                        pc_engine, spec, hourly_symbols=hourly_symbols
                    )
                )
            _rebuild_and_verify_pc_derived_data(
                pc_engine, tickers, affected_daily
            )
        except Exception as exc:
            errors.append(f"PC derived-data verification failed: {exc}")

    # Canonicalize every changed partition from PC -> local.  This applies PC
    # values to conflicts, fills interior gaps, and removes local-only derived
    # or operational rows.  It is deliberately deferred until reverse raw
    # writes and any necessary derived rebuild have succeeded.  One SQLite
    # transaction publishes all tables together and reserves the local writer
    # lock through the final equality barrier.
    canonical_pc_before: Dict[
        str, Dict[Tuple[object, ...], _PartitionFingerprint]
    ] = {}
    if not errors:
        local_conn = None
        try:
            local_conn = local_engine.connect()
            if local_engine.dialect.name == "sqlite":
                local_conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                local_conn.begin()

            # Capture every PC table before any canonical copy starts.  A
            # second full snapshot after the copy is the other side of the
            # fence; any concurrent PC writer makes this pass fail safely.
            for spec in reconcile_specs:
                canonical_pc_before[spec.table_name] = (
                    _scoped_partition_fingerprints(
                        pc_engine, spec, hourly_symbols=hourly_symbols
                    )
                )
            if verify_derived:
                for spec in (item for item in reconcile_specs if item.reverse_raw):
                    if _mismatched_partitions(
                        raw_pc_before_derived.get(spec.table_name, {}),
                        canonical_pc_before[spec.table_name],
                    ):
                        raise RuntimeError(
                            f"PC {spec.table_name} changed while derived data "
                            "was being verified"
                        )

            canonical_local_before = {
                spec.table_name: _scoped_partition_fingerprints(
                    local_engine,
                    spec,
                    hourly_symbols=hourly_symbols,
                    connection=local_conn,
                )
                for spec in reconcile_specs
            }
            pending_pc_to_local: Dict[str, int] = {}
            for spec in reconcile_specs:
                pc_fingerprints = canonical_pc_before[spec.table_name]
                local_fingerprints = canonical_local_before[spec.table_name]
                mismatched = _mismatched_partitions(
                    pc_fingerprints, local_fingerprints
                )
                try:
                    pending_pc_to_local[spec.table_name] = (
                        _copy_pc_partitions_to_local_exactly(
                            pc_engine,
                            local_engine,
                            spec,
                            mismatched,
                            pc_fingerprints,
                            local_fingerprints,
                            local_connection=local_conn,
                        )
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"{spec.table_name} PC -> local failed: {exc}"
                    ) from exc

            # Full all-table barrier: PC-before == local-after == PC-after.
            # This catches new partitions, edits to a formerly equal
            # partition, and a local refresh that started after the initial
            # guard.  A moving database is retried while the UI stays local.
            canonical_local_after = {
                spec.table_name: _scoped_partition_fingerprints(
                    local_engine,
                    spec,
                    hourly_symbols=hourly_symbols,
                    connection=local_conn,
                )
                for spec in reconcile_specs
            }
            canonical_pc_after = {
                spec.table_name: _scoped_partition_fingerprints(
                    pc_engine, spec, hourly_symbols=hourly_symbols
                )
                for spec in reconcile_specs
            }
            unstable = []
            for spec in reconcile_specs:
                before = canonical_pc_before[spec.table_name]
                if _mismatched_partitions(
                    before, canonical_local_after[spec.table_name]
                ) or _mismatched_partitions(
                    before, canonical_pc_after[spec.table_name]
                ):
                    unstable.append(spec.table_name)
            if unstable:
                raise RuntimeError(
                    "database changed during reconciliation: "
                    + ", ".join(unstable)
                )

            if local_engine.dialect.name == "sqlite":
                _mark_local_mirror_handoff_clean(local_conn)
            local_conn.commit()
            pc_to_local = pending_pc_to_local
            local_handoff_ready = local_engine.dialect.name == "sqlite"
        except Exception as exc:
            if local_conn is not None:
                try:
                    local_conn.rollback()
                except Exception:
                    pass
            errors.append(f"Atomic PC/local equality barrier failed: {exc}")
        finally:
            if local_conn is not None:
                local_conn.close()

    return LocalMirrorReconciliationResult(
        success=not errors,
        local_to_pc_rows=local_to_pc,
        pc_to_local_rows=pc_to_local,
        affected_daily_symbols=tuple(sorted(affected_daily)),
        affected_hourly_symbols=tuple(sorted(affected_hourly)),
        errors=tuple(errors),
        local_handoff_ready=local_handoff_ready,
    )


