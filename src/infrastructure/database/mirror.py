"""Static compatibility facade for focused local-mirror modules."""

from .mirror_copy import (_copy_scoped_changed_rows_to_local,
                          _partition_fingerprints, _raw_group_watermarks,
                          _save_mirror_sync_checkpoint,
                          sync_local_mirror_from_pc,
                          sync_local_mirror_from_pc_atomic,
                          sync_local_mirror_from_pc_checkpointed,
                          sync_mirror_table)
from .mirror_engine import (_RAW_MIRROR_SPECS, _RECONCILE_TABLE_SPECS,
                            HOURLY_MIRROR_TABLES, LOCAL_MIRROR_DB_PATH,
                            LOCAL_MIRROR_ENABLED_ENV, MIRRORED_TABLES,
                            DataEngineResolution,
                            LocalMirrorNeedsReconciliationError,
                            LocalMirrorReconciliationResult,
                            _local_mirror_enabled,
                            acquire_local_mirror_handoff_guard,
                            init_local_mirror_engine, mirror_table_stats,
                            release_local_mirror_handoff_guard,
                            resolve_data_engine)
from .mirror_freshness import (local_mirror_hourly_is_stale,
                               local_mirror_is_stale)
from .mirror_reconciliation import reconcile_local_mirror_with_pc

__all__ = [name for name in globals() if not name.startswith("_")]
