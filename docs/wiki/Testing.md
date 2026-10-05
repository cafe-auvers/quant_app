# Testing

## Standard checks

```powershell
python -m pip check
python -m compileall main.py src gate1 scripts tests -q
pytest tests -q
python scripts/run_gate1.py --output artifacts/gate1_report.json
```

Focused main behavior regression:

```powershell
pytest tests/test_core_behaviour.py -q
```

Focused finalized-order regression:

```powershell
pytest tests/test_passive_pullback_orb.py tests/test_execution_queue.py tests/test_trade_card_orb_bridge.py -q
pytest tests/test_trading_engine.py tests/test_execution_command_gateway.py tests/test_entry_attempt_manager.py -q
pytest tests/test_buyboard_runtime_worker.py tests/test_eod_trading_service.py -q
```

Focused cross-device projection checks include
`test_readiness_projection_refreshes_on_state_and_write_gate_changes` in the
runtime-worker suite. In the companion web checkout, run the web tests covering
optimistic planning order, authenticated `/live-updates`, connected planning,
and connected operator actions. They must prove rollback and notification
behavior without constructing a broker or execution runtime.

These suites cover the raw breakout/ORB-high passive zone, current-session
candidate validity, fresh-KIS breakout latching, immediate passive submission,
broker-result handling, durable identity/reconciliation, strict
cancel-then-replace upgrades, restart behavior, fill-linked stops, and EOD
cleanup. Keep the behavior assertions synchronized with
[Current Order Logic](https://github.com/cafe-auvers/quant_app/blob/master/docs/current_order_logic.md).

Synthetic performance checks:

```powershell
python scripts/benchmark_performance.py --sidebar-rows 6000 --db-symbols 2000 --samples 20
```

## Test boundaries

Normal database/migration regressions use isolated fixtures: `pytest
tests/test_coordination_store_migration.py -q`. The opt-in
`tests/test_postgresql_coordination_integration.py` suite uses private
`QUANT_POSTGRES_TEST_CONFIG` and `QUANT_POSTGRES_TEST_PASSWORD` values and creates
disposable QA schemas/roles. Run it only against an explicitly authorized test connection
with provisioning privileges; never point the test schema at production `quant_coordination`
or print credentials. The 2026-10-05 migration release passed 3,283 supported tests (10
opt-in skips), 7 cloud PostgreSQL tests, and all five exact-commit CI checks. These are
dated release results, not evidence for subsequent code changes.

Normal tests must not require a developer MySQL instance, KIS credentials, a
live broker, or Internet access. Use in-memory SQLite, temporary paths, fakes,
and recorded redacted protocol fixtures. A test named for broker behavior is
still not proof of a credentialed production contract check unless explicitly
run under its separate operational procedure.

## CI

GitHub Actions runs compile and pytest on Windows/Python 3.11 and 3.12, then a
deterministic Gate 1 simulation. Branch protection should require both matrix
checks and `Gate 1 deterministic simulation`.

Do not delete, suppress, or weaken a test to obtain a green result. Trading
behavior changes need characterization and boundary tests.

For current host, shared-store, and backup prerequisites, see
[Supabase Deployment](Supabase-Deployment).
