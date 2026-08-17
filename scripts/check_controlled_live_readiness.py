"""Fail-closed, non-mutating preflight for the supervised Kanban pilot.

This command performs no KIS connection and no broker mutation.  It validates
that the local checkout, repository-level .env, capability bundle, WebSocket
composition inputs, controlled-live envelope, and canonical MySQL connection
are internally ready for the *application* to start its guarded runtime.

Usage (from the repository root)::

    python scripts/check_controlled_live_readiness.py

A zero exit code means configuration/preflight passed.  The in-application
trading switch still starts OFF and must be armed explicitly after the runtime
reaches ACTIVE with fresh broker/market-data readiness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable

from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_repo_env() -> None:
    from src.utils.config import load_env_file

    for key, value in load_env_file().items():
        os.environ.setdefault(key, value)


_load_repo_env()

from src.core import execution_config  # noqa: E402
from src.services.controlled_live_policy import (  # noqa: E402
    CONTROLLED_LIVE,
    require_controlled_live_configuration,
)
from src.services.kis_realtime_market_data import (  # noqa: E402
    SubscriptionPriority,
    build_kis_realtime_market_data_from_environment,
)
from src.services.kis_request_scheduler import KisRequestScheduler  # noqa: E402
from src.services.trading_state import is_trading_locked_disabled  # noqa: E402
from src.infrastructure.database.engine import init_mysql_engine  # noqa: E402


class Preflight:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.passes: list[str] = []

    def check(self, name: str, predicate: bool, failure: str) -> None:
        if predicate:
            self.passes.append(name)
        else:
            self.failures.append(f"{name}: {failure}")

    def guarded(self, name: str, operation: Callable[[], None]) -> None:
        try:
            operation()
        except Exception as exc:  # noqa: BLE001 - report exact operational blocker
            self.failures.append(f"{name}: {exc}")
        else:
            self.passes.append(name)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _configured(name: str) -> bool:
    return bool(str(os.getenv(name, "") or "").strip())


def _missing_external_delivery_configuration() -> tuple[str, ...]:
    return tuple(
        name
        for name in (
            "EXTERNAL_ALERT_WEBHOOK_URL",
            "EXTERNAL_HEARTBEAT_WEBHOOK_URL",
        )
        if not _configured(name)
    )


def main() -> int:
    result = Preflight()

    try:
        head = _git("rev-parse", "HEAD").lower()
        dirty = bool(_git("status", "--porcelain"))
    except Exception as exc:  # noqa: BLE001
        result.failures.append(f"git checkout: {exc}")
        head = ""
        dirty = True

    result.check(
        "clean checkout",
        bool(head) and not dirty,
        "controlled live must run from a clean exact commit",
    )
    result.check(
        "runtime SHA pin",
        bool(head) and os.getenv("KIS_RUNTIME_COMMIT_SHA", "").strip().lower() == head,
        "KIS_RUNTIME_COMMIT_SHA must equal the current git HEAD",
    )

    for name in (
        "KIS_PROD_BASE_URL",
        "KIS_PROD_APP_KEY",
        "KIS_PROD_APP_SECRET",
        "KIS_PROD_ACCOUNT_NO",
        "KIS_PROD_WS_URL",
        "KIS_CAPABILITY_MANIFEST_PATH",
        "KIS_CAPABILITY_MANIFEST_SHA256",
    ):
        result.check(name, _configured(name), f"{name} is missing")

    result.check(
        "administrative trading permission",
        _truthy(os.getenv("TRADING_ENABLED", "")) and not is_trading_locked_disabled(),
        "set TRADING_ENABLED=true; the in-app trading switch will still start OFF",
    )
    result.check(
        "Kanban engine flag",
        bool(execution_config.is_buyboard_engine_enabled()),
        "set BUYBOARD_ENGINE_ENABLED=true",
    )
    result.check(
        "controlled-live mode",
        execution_config.KIS_LIVE_EXECUTION_MODE == CONTROLLED_LIVE,
        "set KIS_LIVE_EXECUTION_MODE=CONTROLLED_LIVE",
    )
    result.check(
        "WebSocket mode",
        bool(execution_config.KIS_WS_ENABLED)
        and bool(execution_config.KIS_WS_PROTOCOL_VERIFIED)
        and execution_config.KIS_MARKET_DATA_MODE == "WEBSOCKET",
        "KIS_WS_ENABLED=true, KIS_WS_PROTOCOL_VERIFIED=true, and KIS_MARKET_DATA_MODE=WEBSOCKET are required",
    )
    result.check(
        "aggregate WebSocket capacity",
        int(execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY) > 0,
        "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY must be positive",
    )
    result.check(
        "controlled symbols",
        bool(execution_config.KIS_CONTROLLED_LIVE_SYMBOLS),
        "KIS_CONTROLLED_LIVE_SYMBOLS is empty",
    )
    result.check(
        "entry notional cap",
        float(execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL) > 0,
        "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL must be positive",
    )

    def _check_symbol_map() -> None:
        payload = json.loads(os.getenv("KIS_WS_SYMBOL_KEYS_JSON", "{}") or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("KIS_WS_SYMBOL_KEYS_JSON must be a JSON object")
        normalized = {
            str(symbol).upper(): str(key or "").strip()
            for symbol, key in payload.items()
        }
        missing = [
            symbol
            for symbol in execution_config.KIS_CONTROLLED_LIVE_SYMBOLS
            if not normalized.get(symbol)
        ]
        if missing:
            raise RuntimeError(
                "missing verified WebSocket keys for: " + ", ".join(sorted(missing))
            )

    result.guarded("controlled symbol WebSocket keys", _check_symbol_map)

    scheduler = KisRequestScheduler(
        max_confirmed_mutation_attempts=(
            execution_config.KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS
        ),
        min_mutation_spacing_seconds=(
            execution_config.KIS_MUTATION_MIN_SPACING_SECONDS
        ),
    )
    result.guarded(
        "controlled-live execution envelope",
        lambda: require_controlled_live_configuration(
            environment="PROD", scheduler=scheduler
        ),
    )

    def _compose_websocket_without_connecting() -> None:
        service = build_kis_realtime_market_data_from_environment(
            environment="PROD",
        )
        priority = {
            symbol: int(SubscriptionPriority.CRITICAL_EXIT)
            for symbol in execution_config.KIS_CONTROLLED_LIVE_SYMBOLS
        }
        service.configure_desired_channels(
            trade_priorities=priority,
            quote_priorities=priority,
        )
        capacity = service.subscription_capacity_snapshot()
        if capacity.reconnect_replay_count <= 0:
            raise RuntimeError("no controlled-live WebSocket registrations were composed")
        if capacity.reconnect_replay_count > capacity.total_capacity:
            raise RuntimeError("configured WebSocket registrations exceed total capacity")

    result.guarded(
        "reviewed production WebSocket composition (no connection)",
        _compose_websocket_without_connecting,
    )

    def _check_mysql() -> None:
        engine = init_mysql_engine(log_unavailable=False, ensure_schema=False)
        if engine is None:
            raise RuntimeError("configured PC MySQL is unavailable")
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()

    result.guarded("canonical MySQL connectivity", _check_mysql)

    missing_delivery = _missing_external_delivery_configuration()
    result.check(
        "external alert and heartbeat delivery",
        not missing_delivery,
        "missing " + ", ".join(missing_delivery),
    )

    if not _configured("KIS_WS_HTS_ID"):
        result.warn(
            "KIS_WS_HTS_ID is absent. This does not block the supervised pilot "
            "when execution-notice capability is omitted; REST reconciliation remains authoritative."
        )
    print("Controlled-live preflight")
    print(f"  git head: {head or '<unknown>'}")
    for name in result.passes:
        print(f"  [PASS] {name}")
    for warning in result.warnings:
        print(f"  [WARN] {warning}")
    for failure in result.failures:
        print(f"  [FAIL] {failure}")

    if result.failures:
        print(f"\nNOT READY: {len(result.failures)} blocking item(s).")
        return 1

    print(
        "\nREADY FOR APPLICATION STARTUP (supervised controlled-live configuration).\n"
        "The script did not connect to KIS and did not submit/cancel any order.\n"
        "Launch the app, wait for the Buy Board runtime to reach ACTIVE with fresh "
        "market/account readiness, then arm the in-session trading switch explicitly."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
