"""Read-only production health checks for the desktop dashboard."""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from sqlalchemy import text

from src.api.kis_account_snapshot_dual import KisEnvironment, load_config
from src.core.order_state import BrokerOrder, OrderStatus, is_open_status
from src.infrastructure.database.mirror_freshness import (
    local_mirror_hourly_is_stale,
    local_mirror_is_stale,
)
from src.services.event_journal import (
    MAX_JOURNAL_ARCHIVES,
    EventJournalStatus,
    inspect_event_journal,
    scrub_sensitive_text,
)
from src.utils.market_calendar import expected_latest_market_data_date

KIS_API_HEALTH_MAX_AGE = dt.timedelta(minutes=15)
JOURNAL_DISK_CRITICAL_BYTES = 512 * 1024 * 1024
JOURNAL_DISK_WARNING_BYTES = 2 * 1024 * 1024 * 1024


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
    pc_database_engine: Any = None
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
    # Cross-machine main-device handoff (laptop <-> PC).
    is_main_device: bool = False
    main_device_hostname: str = ""
    lease_age_seconds: Optional[float] = None
    auto_claim_enabled: bool = False
    handoff_reconciliation_running: bool = False
    handoff_reconciliation_required: bool = False
    handoff_blocked_symbols: Sequence[str] = field(default_factory=tuple)
    market_data_metrics: Any = None
    request_scheduler_metrics: Any = None


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
                scrub_sensitive_text(exc),
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
                scrub_sensitive_text(exc),
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


def _parse_health_timestamp(value: str) -> Optional[dt.datetime]:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _kis_api_check(
    context: HealthContext,
    configured: bool,
    *,
    now: Optional[dt.datetime] = None,
) -> HealthCheck:
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
            scrub_sensitive_text(context.kis_last_error),
        )
    if context.kis_snapshot_count:
        last_success = _parse_health_timestamp(context.kis_last_success_at)
        if last_success is None:
            return HealthCheck(
                "KIS API",
                HealthLevel.UNKNOWN,
                f"{context.kis_snapshot_count} cached snapshot(s) have no verified age",
                "Refresh the KIS account snapshot before relying on this status.",
            )
        current = now or dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=dt.timezone.utc)
        age = current.astimezone(dt.timezone.utc) - last_success
        if age < dt.timedelta(minutes=-1):
            return HealthCheck(
                "KIS API",
                HealthLevel.UNKNOWN,
                "KIS verification timestamp is in the future",
                f"Recorded success: {context.kis_last_success_at}.",
            )
        age_seconds = max(0, int(age.total_seconds()))
        if age > KIS_API_HEALTH_MAX_AGE:
            return HealthCheck(
                "KIS API",
                HealthLevel.WARNING,
                f"Last verified response is {age_seconds // 60} minute(s) old",
                f"Last success: {context.kis_last_success_at}. Refresh before trading.",
            )
        return HealthCheck(
            "KIS API",
            HealthLevel.HEALTHY,
            f"Verified {age_seconds} second(s) ago",
            f"{context.kis_snapshot_count} account snapshot(s); last success: "
            f"{context.kis_last_success_at}.",
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
    if context.pc_database_engine is not None:
        try:
            with context.pc_database_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            return HealthCheck(
                "MySQL",
                HealthLevel.CRITICAL,
                "Current read-only database probe failed",
                scrub_sensitive_text(exc),
            )
        return HealthCheck(
            "MySQL",
            HealthLevel.HEALTHY,
            "Primary database verified now",
            "A read-only SELECT 1 probe succeeded.",
        )
    if context.pc_database_ready or context.db_source == "pc":
        return HealthCheck(
            "MySQL",
            HealthLevel.WARNING,
            "Primary database was last known reachable",
            "No engine was available for a current read-only probe.",
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


def _market_data_check(metrics: Any) -> HealthCheck:
    """Project Workstream 5 metrics onto the existing Health tab."""
    if metrics is None:
        return HealthCheck(
            "KIS market data",
            HealthLevel.UNKNOWN,
            "Real-time market-data service is not running",
        )
    missing_trade = tuple(metrics.critical_trade_channels_missing)
    missing_quote = tuple(metrics.critical_quote_channels_missing)
    stale = tuple(metrics.stale_symbols)
    detail = (
        f"trade ACK {metrics.trade_channels_acked}/{metrics.trade_channels_desired}; "
        f"quote ACK {metrics.quote_channels_acked}/{metrics.quote_channels_desired}; "
        f"receive lag p50/p95/p99 {metrics.receive_lag_p50_ms:.1f}/"
        f"{metrics.receive_lag_p95_ms:.1f}/{metrics.receive_lag_p99_ms:.1f} ms; "
        f"reconnects {metrics.reconnect_count}; NACKs {metrics.nack_count}; "
        f"malformed {metrics.malformed_frame_count}; queue {metrics.queue_depth}; "
        f"exact duplicates coalesced {metrics.dropped_event_count}."
    )
    if not metrics.ws_connected:
        return HealthCheck(
            "KIS market data",
            HealthLevel.CRITICAL,
            "KIS WebSocket is disconnected",
            detail,
        )
    if missing_trade or missing_quote:
        return HealthCheck(
            "KIS market data",
            HealthLevel.CRITICAL,
            "Critical symbol subscriptions are missing",
            f"trade={','.join(missing_trade) or 'none'}; "
            f"quote={','.join(missing_quote) or 'none'}. {detail}",
        )
    if stale:
        return HealthCheck(
            "KIS market data",
            HealthLevel.WARNING,
            "One or more symbols currently lack an execution-fresh event",
            f"execution_stale={','.join(stale)}. Connection and subscription "
            "ACKs are intact; exact orders for these symbols remain fail closed. "
            f"{detail}",
        )
    return HealthCheck(
        "KIS market data",
        HealthLevel.HEALTHY,
        "WebSocket and critical symbol channels are ready",
        detail,
    )


def _format_bytes(value: int) -> str:
    number = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


def _event_journal_check(status: EventJournalStatus) -> HealthCheck:
    detail = (
        f"Last write: {status.last_write_at or 'none this session'}; "
        f"latest event: {status.latest_event_at or 'none'}; "
        f"active: {_format_bytes(status.active_file_size)}; "
        f"archives: {status.archive_count} ({_format_bytes(status.archive_bytes)}); "
        f"disk free: {_format_bytes(status.available_disk_space)}."
    )
    if status.inspection_error:
        return HealthCheck(
            "Event journal",
            HealthLevel.CRITICAL,
            "Journal storage inspection failed",
            f"{status.inspection_error} {detail}",
        )
    if status.last_error:
        return HealthCheck(
            "Event journal",
            HealthLevel.CRITICAL,
            "Most recent journal operation failed",
            f"{status.last_error} Error time: {status.last_error_at}. {detail}",
        )
    if not status.directory_writable:
        return HealthCheck(
            "Event journal",
            HealthLevel.CRITICAL,
            "Journal directory is not writable",
            detail,
        )
    if status.available_disk_space < JOURNAL_DISK_CRITICAL_BYTES:
        return HealthCheck(
            "Event journal",
            HealthLevel.CRITICAL,
            "Disk space is critically low",
            detail,
        )
    if not status.lock_available:
        return HealthCheck(
            "Event journal",
            HealthLevel.WARNING,
            "Journal lock is currently busy",
            detail,
        )
    if status.lock_stale:
        return HealthCheck(
            "Event journal",
            HealthLevel.WARNING,
            "A stale journal lock was detected",
            f"The next writer will recover it automatically. {detail}",
        )
    if status.archive_count > MAX_JOURNAL_ARCHIVES:
        return HealthCheck(
            "Event journal",
            HealthLevel.WARNING,
            "Journal archive retention is overdue",
            detail,
        )
    if status.available_disk_space < JOURNAL_DISK_WARNING_BYTES:
        return HealthCheck(
            "Event journal",
            HealthLevel.WARNING,
            "Journal disk space is getting low",
            detail,
        )
    if not status.last_write_at and not status.latest_event_at:
        return HealthCheck(
            "Event journal",
            HealthLevel.UNKNOWN,
            "Journal is writable but no event exists yet",
            detail,
        )
    return HealthCheck(
        "Event journal",
        HealthLevel.HEALTHY,
        "Journal storage is operational",
        detail,
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
                scrub_sensitive_text(exc),
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
            scrub_sensitive_text(exc),
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
            scrub_sensitive_text(context.order_ledger_error),
        )
    ledger_open_orders = [
        order for order in context.orders if is_open_status(order.status)
    ]
    # The application's automatic reconciliation loop intentionally processes
    # production orders only.  Keep Health on that same operational scope so
    # old simulation fixtures cannot create a permanent production warning.
    open_orders = [
        order
        for order in ledger_open_orders
        if str(order.environment or "").strip().upper() == "PROD"
    ]
    ignored_non_production = len(ledger_open_orders) - len(open_orders)
    ignored_detail = (
        f"Ignored {ignored_non_production} non-production open ledger order(s)."
        if ignored_non_production
        else ""
    )

    def detail_with_scope(detail: str = "") -> str:
        return " ".join(part for part in (detail, ignored_detail) if part)

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
            ignored_detail,
        )
    if context.reconciliation_last_error:
        return HealthCheck(
            "Reconciliation",
            HealthLevel.CRITICAL,
            "Most recent reconciliation failed",
            scrub_sensitive_text(context.reconciliation_last_error),
        )
    if unknown_orders:
        symbols = ", ".join(sorted({order.symbol for order in unknown_orders})[:8])
        return HealthCheck(
            "Reconciliation",
            HealthLevel.CRITICAL,
            f"{len(unknown_orders)} order(s) have unknown broker state",
            detail_with_scope(f"Reconcile before retrying: {symbols}."),
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
            detail_with_scope(detail),
        )
    return HealthCheck(
        "Reconciliation",
        HealthLevel.HEALTHY,
        "No unresolved production broker orders",
        detail_with_scope(
            f"Last success: {context.reconciliation_last_success_at}."
            if context.reconciliation_last_success_at
            else ""
        ),
    )


def _main_device_handoff_check(context: HealthContext) -> HealthCheck:
    """Surface cross-machine main-device/lease state without digging through logs."""
    if context.handoff_reconciliation_running:
        return HealthCheck(
            "Main-device handoff",
            HealthLevel.UNKNOWN,
            "Reconciling against the broker before resuming monitoring",
            "Automatic handoff in progress -- monitoring and live trading stay off until this clears.",
        )
    if context.handoff_blocked_symbols:
        symbols = ", ".join(sorted(context.handoff_blocked_symbols)[:8])
        return HealthCheck(
            "Main-device handoff",
            HealthLevel.CRITICAL,
            f"{len(context.handoff_blocked_symbols)} symbol(s) blocked pending reconciliation",
            f"Blocked: {symbols}. Monitoring will not resume for these until reconciliation clears.",
        )
    if context.handoff_reconciliation_required:
        return HealthCheck(
            "Main-device handoff",
            HealthLevel.CRITICAL,
            "Trading fenced until handoff reconciliation completes",
            "The device may hold the lease, but broker reconciliation and durable state publication have not both completed.",
        )
    if context.is_main_device:
        detail = (
            f"Lease age: {context.lease_age_seconds:.0f}s."
            if context.lease_age_seconds is not None
            else "Lease age unknown."
        )
        return HealthCheck(
            "Main-device handoff",
            HealthLevel.HEALTHY,
            "This device is the exclusive main device",
            detail,
        )
    owner = context.main_device_hostname or "another device"
    detail = (
        "Automatic claim is armed on this device."
        if context.auto_claim_enabled
        else "This device is pull-only; automatic claim is not armed here."
    )
    return HealthCheck(
        "Main-device handoff",
        HealthLevel.HEALTHY,
        f"Pull-only -- main device is {owner}",
        detail,
    )


def _request_scheduler_check(metrics: Any) -> HealthCheck:
    if metrics is None:
        return HealthCheck(
            "KIS request scheduler",
            HealthLevel.UNKNOWN,
            "Request scheduler is not running",
        )
    queued = int(getattr(metrics, "queued_requests", 0) or 0)
    budget_rejections = int(getattr(metrics, "budget_rejections", 0) or 0)
    uncertain_entries = int(
        getattr(metrics, "uncertain_entry_rejections", 0) or 0
    )
    retries = int(getattr(metrics, "read_retries", 0) or 0)
    mutation_retries = int(
        getattr(metrics, "confirmed_mutation_retries", 0) or 0
    )
    known_budgets = int(
        getattr(metrics, "known_mutation_budget_buckets", 0) or 0
    )
    uncertain_budgets = int(
        getattr(metrics, "uncertain_mutation_budget_buckets", 0) or 0
    )
    detail = (
        f"queue={queued}, budget_rejections={budget_rejections}, "
        f"uncertain_entry_blocks={uncertain_entries}, read_retries={retries}, "
        f"confirmed_mutation_retries={mutation_retries}, "
        f"known_mutation_budgets={known_budgets}, "
        f"uncertain_mutation_budgets={uncertain_budgets}."
    )
    if known_budgets == 0:
        return HealthCheck(
            "KIS request scheduler",
            HealthLevel.WARNING,
            "WS0 mutation budgets are unverified; new entries are closed",
            detail,
        )
    if budget_rejections or uncertain_entries or uncertain_budgets:
        return HealthCheck(
            "KIS request scheduler",
            HealthLevel.WARNING,
            "Requests are being held by rate-budget gates",
            detail,
        )
    return HealthCheck(
        "KIS request scheduler",
        HealthLevel.HEALTHY,
        "Request priorities and budgets are active",
        detail,
    )


def collect_health_snapshot(context: HealthContext) -> HealthSnapshot:
    """Run read-only local probes and combine them with cached runtime state."""
    token_check, configured = inspect_kis_token()
    try:
        journal_check = _event_journal_check(inspect_event_journal())
    except Exception as exc:
        journal_check = HealthCheck(
            "Event journal",
            HealthLevel.CRITICAL,
            "Journal health check failed",
            scrub_sensitive_text(exc),
        )
    checks = [
        token_check,
        _kis_api_check(context, configured),
        _mysql_check(context),
        _mirror_check(context),
        _reconciliation_check(context),
        _main_device_handoff_check(context),
        journal_check,
    ]
    if context.market_data_metrics is not None:
        checks.append(_market_data_check(context.market_data_metrics))
    if context.request_scheduler_metrics is not None:
        checks.append(_request_scheduler_check(context.request_scheduler_metrics))
    checks.append(
        HealthCheck(
            "Application heartbeat",
            HealthLevel.HEALTHY,
            "UI event loop is responsive",
            "This snapshot was produced by a background health probe.",
        )
    )
    return HealthSnapshot(
        checked_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        checks=checks,
    )
