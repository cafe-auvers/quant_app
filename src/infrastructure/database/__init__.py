"""Explicit database infrastructure facade.

New code should import the focused owner module. This facade preserves the
public API while avoiding shared globals, module replacement, and monkeypatch
synchronization.
"""

from .engine import (get_mysql_connection_url, init_mysql_engine,
                     validate_mysql_config, validate_mysql_identifier,
                     validate_mysql_port)
from .mirror import (_RAW_MIRROR_SPECS, _RECONCILE_TABLE_SPECS,
                     HOURLY_MIRROR_TABLES, LOCAL_MIRROR_DB_PATH,
                     LOCAL_MIRROR_ENABLED_ENV, MIRRORED_TABLES,
                     DataEngineResolution, LocalMirrorNeedsReconciliationError,
                     LocalMirrorReconciliationResult,
                     _copy_scoped_changed_rows_to_local, _local_mirror_enabled,
                     _partition_fingerprints, _raw_group_watermarks,
                     _save_mirror_sync_checkpoint,
                     acquire_local_mirror_handoff_guard,
                     init_local_mirror_engine, local_mirror_hourly_is_stale,
                     local_mirror_is_stale, mirror_table_stats,
                     reconcile_local_mirror_with_pc,
                     release_local_mirror_handoff_guard, resolve_data_engine,
                     sync_local_mirror_from_pc,
                     sync_local_mirror_from_pc_atomic,
                     sync_local_mirror_from_pc_checkpointed, sync_mirror_table)
from .refresh import (HOURLY_BACKFILL_PERIOD, _period_for_daily_refresh,
                      _period_for_hourly_refresh,
                      refresh_universe_history_to_db,
                      refresh_universe_hourly_history_to_db)
from .repositories.market_data import (
    _get_chart_indicator_manifests, calculate_chart_indicators,
    calculate_chart_indicators_since, delete_intraday_history_for_symbol,
    get_chart_indicator_refresh_plan, get_latest_chart_indicator_dates,
    get_latest_chart_indicator_source_dates,
    get_latest_hourly_price_history_timestamp,
    get_latest_hourly_price_history_timestamps, get_latest_price_history_date,
    get_latest_price_history_dates, get_price_history_watermarks,
    load_chart_indicators_from_db, load_hourly_history_from_db,
    load_intraday_history_from_db, load_symbol_history_from_db,
    load_universe_history_from_db, prune_intraday_history,
    refresh_chart_indicators_for_symbol, refresh_chart_indicators_to_db,
    save_chart_indicators_batch_to_db, save_chart_indicators_to_db,
    save_hourly_history_to_db, save_intraday_history_to_db,
    save_symbol_history_to_db, save_universe_history_batch_to_db,
    save_universe_hourly_history_batch_to_db)
from .repositories.scanner import (get_universe_stock_metrics_from_db,
                                   is_scanner_metrics_snapshot_current,
                                   load_scanner_metrics_from_db,
                                   refresh_scanner_metrics_to_db,
                                   save_scanner_metrics_batch_to_db,
                                   save_scanner_metrics_snapshot_to_db,
                                   save_scanner_metrics_to_db,
                                   scanner_metrics_input_fingerprint,
                                   scanner_metrics_snapshot_date)
from .repositories.fundamentals import (
    EARNINGS_DATASET, PROFILE_DATASET, ensure_fundamental_tables,
    load_earnings_events, load_fundamental_sync_state,
    load_fundamental_sync_states, load_stock_profile,
    normalized_payload_fingerprint, record_fundamental_sync_state,
    seed_stock_profiles, upsert_earnings_events, upsert_stock_profile)
from .schema import (CHRONIC_FAILURE_THRESHOLD,
                     _ensure_chart_indicator_manifests_table,
                     _ensure_chart_indicators_table,
                     _ensure_earnings_events_table,
                     _ensure_fundamental_sync_state_table,
                     _ensure_hourly_price_history_table,
                     _ensure_intraday_price_history_table,
                     _ensure_price_history_table,
                     _ensure_scanner_metric_snapshots_table,
                     _ensure_scanner_metrics_table,
                     _ensure_stock_profiles_table, _ensured_engines,
                     _get_intraday_price_history_table,
                     _get_price_history_table, get_chronically_failing_symbols,
                     record_symbol_refresh_outcomes)
from .settings import (CACHE_QUERY_SYMBOL_CHUNK_SIZE,
                       CHART_INDICATOR_CACHE_VERSION,
                       HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE,
                       MYSQL_CONNECT_TIMEOUT_SECONDS,
                       MYSQL_POOL_RECYCLE_SECONDS,
                       MYSQL_READ_WRITE_TIMEOUT_SECONDS, REFERENCE_SYMBOL,
                       SCANNER_METRIC_WRITE_CHUNK_SIZE,
                       SCANNER_METRICS_CACHE_VERSION,
                       SCANNER_QUERY_SYMBOL_CHUNK_SIZE)
from .sql_helpers import _execute_bulk_upsert

__all__ = [
    name
    for name in globals()
    if not name.startswith("_")
]
