# Performance Audit

Audit date: 2026-08-23 (KST)

Branch baseline: `feature/populate-stock-profiles` at `51bcc4c`
Runtime used: Windows, Python 3.12.4, PyQt5 offscreen for synthetic UI checks

This audit preserves strategy rules, risk calculations, order behavior, broker
boundaries, state transitions, and fail-closed defaults. Measurements are
synthetic and read-only: no KIS credentials, live endpoints, production MySQL,
or broker mutations are used.

## Measured baseline

The unmodified full suite completed with `2498 passed, 1 warning in 286.36s`.
The warning is an upstream protobuf deprecation warning.

Two deterministic hot paths were measured before changing them:

| Workflow | Representative load | Median | p95 | Evidence |
|---|---:|---:|---:|---|
| Unchanged sidebar refresh, legacy-equivalent forced rebuild | 6,000 symbols, 20 samples | 86.986 ms | 150.296 ms | Every refresh cleared and recreated all `QListWidgetItem` rows |
| Latest daily-cache watermark without covering index | 540,000 in-memory SQLite rows, 20 samples | 147.519 ms | 173.517 ms | Query plan did not name an index and scanned the cache |

An earlier direct run against the unmodified sidebar implementation measured
160.96 ms median and 246.04 ms p95 for 6,000 rows (12 samples). The
same-process forced-rebuild result above is the reproducible comparison used
for the final percentage calculation.

Run the maintained benchmark with:

```powershell
python scripts/benchmark_performance.py --sidebar-rows 6000 --db-symbols 2000 --samples 20
```

## Bottlenecks and root causes

### Unchanged sidebar projections rebuilt every row

Scanner completion, universe loading, and Buy Board projections can request a
sidebar refresh. `refresh_stock_sidebar()` always cleared and reconstructed the
selected source even when every displayed label and payload value was
unchanged. A full universe is several thousand symbols, so object allocation,
Qt model mutation, selection restoration, and chart-selection callbacks were
paid repeatedly.

### Global price watermark lacked a usable index

`get_latest_price_history_date()` filters on `interval` and returns
`MAX(date)`. The existing primary key is `(symbol, date, interval)`, so it
cannot efficiently serve a query that has no symbol predicate. Cache size,
rather than the one-row result size, controlled latency.

### Confirmed remaining synchronous chart work

Static call-path review confirmed that chart navigation still performs local
SQL reads and DataFrame/HTML preparation from the UI callback before calling
`QWebEngineView.setHtml()`. Network fallbacks are configuration/gesture gated,
and fundamental provider refreshes run in workers, but first-use chart history,
indicator, fundamental-cache, and market-alignment reads can still block if the
configured SQL endpoint is slow. No production database was available for a
safe representative latency measurement, so this audit does not claim that
path improved.

### Full widget rebuilds in localized tables

Buylist and health tables still replace their bounded row sets after a new
snapshot. Their slow data acquisition already runs in workers, and their normal
row counts are small compared with the full symbol universe. No target breach
was reproduced locally, so they were left unchanged.

## Changes implemented

- Added a complete sidebar presentation signature covering source identity,
  ordering, labels, price/status/account payloads, and planning fields.
- Equivalent projections now update only Watchlist action availability; they do
  not clear rows or reapply the selected symbol to the active chart.
- Any visible field change invalidates the signature and uses the existing full
  rebuild path, preserving freshness and selection behavior.
- Added the covering index
  `ix_price_history_interval_date(interval, date)`, including idempotent
  creation for an existing cache during explicit schema initialization. Normal
  dashboard reads do not attempt a potentially locking index migration.
- Added structural UI regression tests and a SQLite query-plan regression test.
- Added `scripts/benchmark_performance.py` so the synthetic measurements remain
  reproducible without live services.

## Before and after

Final same-process benchmark output:

| Workflow | Before median / p95 | After median / p95 | Result |
|---|---:|---:|---:|
| Equivalent 6,000-row sidebar projection | 86.986 / 150.296 ms | 8.023 / 12.209 ms | Median 90.8% lower; p95 91.9% lower |
| 540,000-row daily watermark | 147.519 / 173.517 ms | 0.005 / 0.117 ms | Covering-index plan confirmed |

The unchanged sidebar path is below the 100 ms interaction target and the
50 ms long-task threshold in this synthetic workload. A cold 6,000-row build
was 78.807 ms and remains above the 50 ms target; it occurs when a genuinely
different large source must be presented.

## Existing responsiveness controls verified

- Database initialization, market-data status, Market Pulse, scanner, KIS
  account, health, broker query/cancel, Buy Board projection/command, and mirror
  synchronization work use background workers.
- Buy Board projection requests coalesce while a worker or drag interaction is
  active; unchanged card render signatures update live metrics in place.
- Chart refresh keys and TTLs suppress duplicate full chart loads, and hidden
  chart views are marked stale instead of being rebuilt immediately.
- State persistence is coalesced and written by a background save manager with
  atomic local-file replacement and shutdown flushing.
- Intraday fetches are deduplicated/cooldown guarded, and a completed
  single-symbol fetch refreshes only that execution-queue symbol.
- MySQL/coordination engines use pooled SQLAlchemy engines rather than creating
  a new session stack for each UI event.

## Validation

- Final full suite: `2502 passed, 1 warning in 204.00s`.
- Deterministic Gate 1 simulation: `676 passed, 1 warning in 48.01s`.
- The warning in both runs is the same upstream protobuf datetime deprecation.
- New performance files pass Black and Flake8 checks; the changed standalone
  database/benchmark/test modules pass scoped mypy checks.
- Compilation, `pip check`, diff whitespace validation, and Markdown
  structure/relative-link validation pass.

## Limitations and recommended follow-up

1. Move chart cache reads, indicator preparation, and large HTML generation to
   a generation-fenced worker. Keep only the final Qt widget update on the UI
   thread, and add stale-symbol cancellation tests.
2. Replace or batch the cold full-universe `QListWidget` population with a
   model-backed virtualized list if cold source changes are common; preserve the
   current symbol payload contract and keyboard behavior.
3. Measure chart navigation, Buy Board rendering, and local-mirror refresh with
   sanitized production-scale row counts and an instrumented Qt event loop.
4. Capture p50/p95 worker queue delay and UI apply time separately. Do not log
   account numbers, order identifiers, credentials, or raw broker payloads.
5. Validate the new index on production MySQL using `EXPLAIN` during a
   maintenance window before allowing schema initialization to install it. The
   SQLite result proves query shape, not MySQL wall time under real storage and
   concurrency.

Absolute end-to-end latency targets cannot be certified in this checkout
because live MySQL/KIS access and representative operator datasets were
intentionally excluded. The measurements above support only the two changed
paths.
