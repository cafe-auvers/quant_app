"""Read-only production health checks for the desktop dashboard."""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from src.api.kis_account_snapshot_dual import KisEnvironment, load_config
from src.core.order_state import BrokerOrder, OrderStatus, is_open_status
from src.infrastructure.database.mirror_freshness import (
    local_mirror_hourly_is_stale,
    local_mirror_is_stale,
)
from src.utils.market_calendar import expected_latest_market_data_date


class HealthLevel(str, Enum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class HealthCheck:
    component: str
    level: HealthLevel
    summary: str
    detail: str = ""


@dataclass(frozen=True)
class HealthContext:
    db_source: str = "none"
    db_initializing: bool = False
    pc_database_ready: bool = False
    mirror_engine: Any = None
    mirror_tickers: Optional[Sequence[str]] = None
    kis_snapshot_count: int = 0
    kis_request_running: bool = False
    kis_last_success_at: str = ""
    kis_last_error: str = ""
    orders: Sequence[BrokerOrder] = field(default_factory=tuple)
    order_ledger_error: str = ""
    reconciliation_running: bool = False
    reconciliation_last_success_at: str = ""
    reconciliation_last_error: str = ""


@dataclass(frozen=True)
class HealthSnapshot:
    checked_at: str
    checks: List[HealthCheck]

    @property
    def overall_level(self) -> HealthLevel:
        levels = {check.level for check in self.checks}
        if HealthLevel.CRITICAL in levels:
            return HealthLevel.CRITICAL
        if HealthLevel.WARNING in levels:
            return HealthLevel.WARNING
        if HealthLevel.UNKNOWN in levels:
            return HealthLevel.UNKNOWN
        return HealthLevel.HEALTHY


def inspect_kis_token(now_epoch: Optional[int] = None) -> tuple[HealthCheck, bool]:
    """Inspect local KIS configuration/token metadata without contacting KIS."""
    try:
        config = load_config(KisEnvironment.PROD)
    except Exception as exc:
        return (
            HealthCheck(
                "KIS token",
                HealthLevel.CRITICAL,
                "KIS production credentials are incomplete",
                str(exc),
            ),
            False,
        )

    path = config.token_cache_path
    if path is None:
        return (
            HealthCheck(
                "KIS token",
                HealthLevel.WARNING,
                "Token cache is disabled",
                "KIS can authenticate, but every restart may require a new token.",
            ),
            True,
        )
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        expires_at = int(payload.get("expires_at", 0))
    except FileNotFoundError:
        return (
            HealthCheck(
                "KIS token",
                HealthLevel.UNKNOWN,
                "No cached token yet",
                "A token will be requested by the next authenticated KIS call.",
            ),
            True,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return (
            HealthCheck(
                "KIS token",
                HealthLevel.WARNING,
                "Token cache is unreadable",
                str(exc),
            ),
            True,
        )

    now_epoch = int(time.time()) if now_epoch is None else int(now_epoch)
    expires_text = (
        dt.datetime.fromtimestamp(max(0, expires_at), tz=dt.timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )
    if expires_at <= now_epoch + 60:
        return (
            HealthCheck(
                "KIS token",
                HealthLevel.WARNING,
                "Cached token is expired or near expiry",
                f"Cached expiry: {expires_text}. KIS will request a replacement.",
            ),
            True,
        )
    return (
        HealthCheck(
            "KIS token",
            HealthLevel.HEALTHY,
            "Cached token is valid",
            f"Cached expiry: {expires_text}.",
        ),
        True,
    )


def _kis_api_check(context: HealthContext, configured: bool) -> HealthCheck:
    if not configured:
        return HealthCheck(
            "KIS API",
            HealthLevel.CRITICAL,
            "KIS API is not configured",
            "Configure production credentials before enabling live execution.",
        )
    if context.kis_request_running:
        return HealthCheck(
            "KIS API", HealthLevel.UNKNOWN, "Read-only account request is running"
        )
    if context.kis_last_error:
        return HealthCheck(
            "KIS API",
            HealthLevel.CRITICAL,
            "Most recent account request failed",
            context.kis_last_error,
        )
    if context.kis_snapshot_count:
        suffix = (
            f" Last success: {context.kis_last_success_at}."
            if context.kis_last_success_at
            else ""
        )
        return HealthCheck(
            "KIS API",
            HealthLevel.HEALTHY,
            f"{context.kis_snapshot_count} account snapshot(s) loaded",
            suffix.strip(),
        )
    return HealthCheck(
        "KIS API",
        HealthLevel.UNKNOWN,
        "No account response has been observed this session",
        "Health refresh does not call the broker; use the Dashboard's KIS refresh to verify it.",
    )


def _mysql_check(context: HealthContext) -> HealthCheck:
    if context.db_initializing:
        return HealthCheck("MySQL", HealthLevel.UNKNOWN, "Database probe is running")
    if context.pc_database_ready or context.db_source == "pc":
        return HealthCheck(
            "MySQL", HealthLevel.HEALTHY, "Primary database is reachable"
        )
    if context.db_source == "local_mirror":
        return HealthCheck(
            "MySQL",
            HealthLevel.WARNING,
            "Primary database is unavailable",
            "The dashboard is using the local read-only data mirror.",
        )
    return HealthCheck(
        "MySQL",
        HealthLevel.WARNING,
        "No database source is available",
        "Database-backed scanning and cache freshness are degraded.",
    )


def _mirror_check(context: HealthContext) -> HealthCheck:
    if context.mirror_engine is None:
        return HealthCheck(
            "Data mirror",
            HealthLevel.UNKNOWN,
            "Local mirror is not available",
            "Mirror freshness could not be checked.",
        )
    expected_date = expected_latest_market_data_date()
    tickers: Optional[Iterable[str]] = context.mirror_tickers or None
    if tickers is None:
        try:
            from src.utils.data_loader import get_default_universe

            tickers = get_default_universe()
        except Exception as exc:
            return HealthCheck(
                "Data mirror",
                HealthLevel.WARNING,
                "Tracked universe could not be loaded",
                str(exc),
            )
    try:
        daily_stale = local_mirror_is_stale(
            context.mirror_engine, expected_date, tickers=tickers
        )
        hourly_stale = local_mirror_hourly_is_stale(
            context.mirror_engine, expected_date, tickers=tickers
        )
    except Exception as exc:
        return HealthCheck(
            "Data mirror",
            HealthLevel.WARNING,
            "Mirror freshness check failed",
            str(exc),
        )
    stale_parts = [
        label
        for label, stale in (("daily", daily_stale), ("hourly", hourly_stale))
        if stale
    ]
    if stale_parts:
        return HealthCheck(
            "Data mirror",
            HealthLevel.WARNING,
            f"{', '.join(stale_parts).title()} history is stale",
            f"Market data is expected through {expected_date}.",
        )
    return HealthCheck(
        "Data mirror",
        HealthLevel.HEALTHY,
        "Daily and hourly mirror data are current",
        f"Market data is present through the expected date {expected_date}.",
    )


def _reconciliation_check(context: HealthContext) -> HealthCheck:
    if context.order_ledger_error:
        return HealthCheck(
            "Reconciliation",
            HealthLevel.CRITICAL,
            "Local order ledger is unreadable",
            context.order_ledger_error,
        )
    open_orders = [order for order in context.orders if is_open_status(order.status)]
    unknown_orders = [
        order
        for order in open_orders
        if order.status in {OrderStatus.UNKNOWN, OrderStatus.UNKNOWN_SUBMISSION_STATE}
    ]
    if context.reconciliation_running:
        return HealthCheck(
            "Reconciliation",
            HealthLevel.UNKNOWN,
            f"Reconciling {len(open_orders)} open order(s)",
        )
    if context.reconciliation_last_error:
        return HealthCheck(
            "Reconciliation",
            HealthLevel.CRITICAL,
            "Most recent reconciliation failed",
            context.reconciliation_last_error,
        )
    if unknown_orders:
        symbols = ", ".join(sorted({order.symbol for order in unknown_orders})[:8])
        return HealthCheck(
            "Reconciliation",
            HealthLevel.CRITICAL,
            f"{len(unknown_orders)} order(s) have unknown broker state",
            f"Reconcile before retrying: {symbols}.",
        )
    if open_orders:
        detail = (
            f"Last success: {context.reconciliation_last_success_at}."
            if context.reconciliation_last_success_at
            else "Awaiting the next broker reconciliation cycle."
        )
        return HealthCheck(
            "Reconciliation",
            HealthLevel.WARNING,
            f"{len(open_orders)} open order(s) await final state",
            detail,
        )
    return HealthCheck(
        "Reconciliation",
        HealthLevel.HEALTHY,
        "No unresolved local broker orders",
        (
            f"Last success: {context.reconciliation_last_success_at}."
            if context.reconciliation_last_success_at
            else ""
        ),
    )


def collect_health_snapshot(context: HealthContext) -> HealthSnapshot:
    """Run read-only local probes and combine them with cached runtime state."""
    token_check, configured = inspect_kis_token()
    return HealthSnapshot(
        checked_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        checks=[
            token_check,
            _kis_api_check(context, configured),
            _mysql_check(context),
            _mirror_check(context),
            _reconciliation_check(context),
            HealthCheck(
                "Application heartbeat",
                HealthLevel.HEALTHY,
                "UI event loop is responsive",
                "This snapshot was produced by a background health probe.",
            ),
        ],
    )
