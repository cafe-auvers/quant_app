"""Pure presentation model for Buy Board runtime readiness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from src.core import execution_config
from src.core.runtime_readiness import EngineReadiness, RuntimeDeviceState
from src.services.controlled_live_policy import controlled_live_symbols

_STANDBY_GATE_LABELS = {
    "startup_reconciliation_complete": "initial broker reconciliation",
    "account_reconciliation_fresh": "fresh broker account snapshot",
    "websocket_connected": "KIS WebSocket connection",
    "critical_trade_subscriptions_acked": "trade subscription acknowledgements",
    "critical_quote_subscriptions_acked": "quote subscription acknowledgements",
    "accumulator_draining_within_budget": "sustained market-data queue delay",
    "database_writable": "local Kanban operational state writable",
}


@dataclass(frozen=True)
class BuyboardReadinessDisplay:
    completed: int
    total: int
    label: str
    tooltip: str
    indeterminate: bool = False


def format_readiness_eta(seconds: float) -> str:
    remaining = max(0, int(seconds))
    days, remainder = divmod(remaining, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    prefix = f"{days}d " if days else ""
    return f"{prefix}{hours:02d}:{minutes:02d}:{seconds:02d}"


def live_execution_status_text(enabled: bool, *, engine=None) -> str:
    """Describe both the shared switch and the configured broker envelope."""

    switch = "Enabled" if bool(enabled) else "Disabled"
    mode = str(execution_config.KIS_LIVE_EXECUTION_MODE or "DISABLED").upper()
    if mode != "CONTROLLED_LIVE":
        return f"{switch} ({mode})"
    symbols = ",".join(controlled_live_symbols(engine=engine)) or "none"
    cap = float(execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL or 0.0)
    equity_fraction = float(
        execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_EQUITY_FRACTION or 0.0
    )
    cap_labels = []
    if cap > 0:
        cap_labels.append(f"${cap:,.2f}")
    if equity_fraction > 0:
        cap_labels.append(f"{equity_fraction:.0%} current NAV")
    cap_text = " and ".join(cap_labels) or "blocked"
    return (
        f"{switch} (CONTROLLED_LIVE active cards: {symbols}, " f"max {cap_text}/entry)"
    )


def _per_symbol_quote_guard_detail(
    readiness: EngineReadiness,
    *,
    regular_session_open: bool,
    seconds_until_open: Optional[float],
) -> str:
    if readiness.critical_quotes_fresh:
        return ""
    detail = (
        "Per-symbol execution guard: symbols without a fresh execution-grade "
        "quote remain individually blocked; overall board readiness is unchanged."
    )
    if not regular_session_open and seconds_until_open is not None:
        detail += f" Market opens in {format_readiness_eta(seconds_until_open)}."
    return detail


def buyboard_readiness_display(
    readiness: EngineReadiness,
    *,
    device_state: RuntimeDeviceState,
    reconciliation_accounts: Tuple[str, ...] = (),
    regular_session_open: bool = False,
    seconds_until_open: Optional[float] = None,
    auto_claim_enabled: bool = False,
    is_main_device: bool = False,
    single_session_handoff_ready: bool = False,
) -> BuyboardReadinessDisplay:
    """Project the authoritative readiness predicate into operator language."""

    checks = readiness.standby_check_results
    completed = readiness.standby_checks_completed
    total = len(checks)
    blockers = readiness.standby_blockers
    blocked_labels = tuple(_STANDBY_GATE_LABELS[item] for item in blockers)
    quote_guard_detail = _per_symbol_quote_guard_detail(
        readiness,
        regular_session_open=regular_session_open,
        seconds_until_open=seconds_until_open,
    )

    if device_state == RuntimeDeviceState.ACTIVE:
        tooltip_parts = [
            "Startup readiness is latched while the worker remains ACTIVE.",
            "Every broker mutation is revalidated for its account, symbol, and action.",
        ]
        if blocked_labels:
            tooltip_parts.append("Current action guards: " + ", ".join(blocked_labels))
        if quote_guard_detail:
            tooltip_parts.append(quote_guard_detail)
        return BuyboardReadinessDisplay(
            total,
            total,
            f"Buy Board readiness {total}/{total} — ACTIVE; "
            "broker mutations remain guarded by Live Trading",
            " | ".join(tooltip_parts),
        )
    if single_session_handoff_ready:
        return BuyboardReadinessDisplay(
            total,
            total,
            f"Buy Board readiness {total}/{total} — STANDBY_READY; "
            "KIS WebSocket transfers with Execution Owner",
            "KIS permits one realtime socket for this app key. The current "
            "ACTIVE executor still has a healthy feed; this device has passed "
            "database and broker reconciliation and will remain execution-closed "
            "until its own socket connects after the fenced owner transfer.",
        )
    if reconciliation_accounts:
        accounts = ", ".join(reconciliation_accounts)
        reason = f"final broker reconciliation for {accounts} (ETA unavailable)"
        return BuyboardReadinessDisplay(
            completed,
            total,
            f"Buy Board startup — {reason}",
            "A live broker query is in progress. Its completion time depends on KIS response latency.",
            indeterminate=True,
        )
    if not readiness.startup_reconciliation_complete:
        reason = "initial broker reconciliation (ETA unavailable)"
        return BuyboardReadinessDisplay(
            completed,
            total,
            f"Buy Board startup — {reason}",
            "Startup cannot become ready until every configured account has been reconciled.",
            indeterminate=True,
        )
    if len(blocked_labels) > 1:
        reason = f"{len(blocked_labels)} checks pending: " + "; ".join(blocked_labels)
    elif not readiness.database_writable:
        reason = "waiting for the local Kanban operational store"
    elif not readiness.account_reconciliation_fresh:
        reason = "waiting for a fresh broker account snapshot"
    elif not readiness.websocket_connected:
        reason = "connecting to the KIS WebSocket"
    elif not readiness.critical_trade_subscriptions_acked:
        reason = "waiting for KIS trade-subscription ACKs"
    elif not readiness.critical_quote_subscriptions_acked:
        reason = "waiting for KIS quote-subscription ACKs"
    elif not readiness.accumulator_draining_within_budget:
        reason = "market-data queue missed its drain budget three times"
    elif device_state == RuntimeDeviceState.STANDBY_READY:
        reason = (
            "STANDBY_READY; automatic PC claim is armed"
            if auto_claim_enabled
            else "STANDBY_READY; use this device as Main"
        )
    elif readiness.standby_ready:
        reason = (
            "Main lease held; per-symbol execution guards active"
            if is_main_device
            else "publishing final readiness confirmation"
        )
    else:
        reason = "checking execution dependencies"

    passed_labels = tuple(
        _STANDBY_GATE_LABELS[field_name] for field_name, passed in checks if passed
    )
    tooltip_parts = []
    if passed_labels:
        tooltip_parts.append("Passed: " + ", ".join(passed_labels))
    if blocked_labels:
        tooltip_parts.append("Waiting: " + ", ".join(blocked_labels))
    if quote_guard_detail:
        tooltip_parts.append(quote_guard_detail)
    tooltip_parts.append(
        "Automatic PC claim is enabled."
        if auto_claim_enabled
        else "Automatic execution-owner claim is disabled."
    )
    return BuyboardReadinessDisplay(
        completed,
        total,
        f"Buy Board readiness {completed}/{total} — {reason}",
        " | ".join(tooltip_parts),
    )


# Stable compatibility aliases while callers move to the descriptive names.
_buyboard_readiness_display = buyboard_readiness_display
_format_readiness_eta = format_readiness_eta
_live_execution_status_text = live_execution_status_text


__all__ = [
    "BuyboardReadinessDisplay",
    "buyboard_readiness_display",
    "format_readiness_eta",
    "live_execution_status_text",
]
