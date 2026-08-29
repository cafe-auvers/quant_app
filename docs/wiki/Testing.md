# Testing

## Standard checks

```powershell
python -m pip check
python -m compileall main.py src gate1 scripts tests -q
pytest -q
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
