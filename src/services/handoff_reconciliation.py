"""Pre-resume broker reconciliation for cross-machine main-device handoff.

The single most safety-critical piece of the automatic laptop<->PC handoff
feature. Before a newly-main device resumes monitoring/auto-submission, it
must confirm -- against the broker directly, never against synced local
state -- that nothing is already in flight for each in-flight PROD symbol.

Two facts make this necessary rather than optional:

1. ``_buy_order_pending``/``_stop_order_pending``/``_exit_order_pending`` are
   runtime-only attributes on ``BuylistItem`` (never dataclass fields, never
   serialized). A ``BuylistItem`` freshly built by ``BuylistManager.from_dict``
   after a state-sync pull simply doesn't have them, so every
   ``getattr(item, "_buy_order_pending", False)`` guard in
   ``src/ui/buylist/monitoring.py`` silently reads False -- "nothing
   pending" -- regardless of what was actually in flight on the other
   device.
2. The local order ledger (``orders.json``) and event journal
   (``event_journal.jsonl``) are both per-device files, not among
   ``state_sync.SYNCED_STATE_KEYS``. A newly-main device has zero visibility
   into an order the *other* device already submitted unless it asks the
   broker directly.

``reset_runtime_only_order_flags`` flips the dangerous default (assume
nothing pending) to the safe one (assume something might be pending until
proven otherwise) the instant a device becomes main, before anything else
runs. ``run_post_claim_broker_reconciliation`` then clears that assumption
per-symbol only once the broker snapshot is unambiguous.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.core.execution_queue import (
    HANDOFF_MONITORABLE_STATUSES,
    POSITION_HOLDING_STATUSES,
    PRE_ENTRY_QUEUE_STATUSES,
)
from src.core.order_state import BrokerOrderDiscoveryResult, OrderStatus, is_open_status
from src.services.broker import Broker, KisBroker
from src.services.event_journal import EventType, record_event
# Reused rather than reimplemented -- this is the same holdings-lookup logic
# order_reconciliation.py already uses for broker-truth position checks.
from src.services.order_reconciliation import _holding_for_symbol

logger = logging.getLogger(__name__)


@dataclass
class PostClaimReconciliationResult:
    ok: bool
    reconciled_symbols: List[str] = field(default_factory=list)
    blocked_symbols: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def in_flight_buylist_items(buylist_manager, *, environment: str = "PROD") -> List[Any]:
    """PROD items whose status needs a runtime-flag reset + broker reconciliation."""
    if buylist_manager is None:
        return []
    return [
        item
        for item in getattr(buylist_manager, "items", [])
        if str(getattr(item, "environment", "") or "").upper() == environment
        and str(getattr(item, "monitoring_status", "") or "").upper()
        in HANDOFF_MONITORABLE_STATUSES
    ]


def reset_runtime_only_order_flags(buylist_manager, *, environment: str = "PROD") -> List[Any]:
    """Force every in-flight item's runtime pending flags to True.

    Must run synchronously, immediately, the moment a device becomes main --
    before the monitor timer or any auto-submission path can touch these
    items. Returns the items it touched (for logging/state-save callers).
    """
    items = in_flight_buylist_items(buylist_manager, environment=environment)
    for item in items:
        item._buy_order_pending = True
        item._stop_order_pending = True
        item._exit_order_pending = True
    return items


def run_post_claim_broker_reconciliation(
    buylist_manager,
    *,
    environment: str = "PROD",
    broker: Optional[Broker] = None,
    event_recorder: Optional[Callable[..., Any]] = None,
) -> PostClaimReconciliationResult:
    """Reconcile every in-flight PROD item against broker truth.

    Uses broker-truth discovery, not the local order ledger. Items are
    partitioned by their persisted ``kis_account_no`` and each account's
    regular open/history and reserved-order ledgers are queried in full.

    A symbol only clears (flags reset to False) when the broker snapshot is
    unambiguous: no open order for that symbol, and holdings agree with (or
    are safely corrected from) local state. Any open order, disagreement, or
    API error leaves that symbol's flags locked True in
    ``blocked_symbols`` -- the caller must not start the monitor or arm
    trading while any symbol remains blocked.
    """
    broker = KisBroker() if broker is None else broker
    items = in_flight_buylist_items(buylist_manager, environment=environment)
    result = PostClaimReconciliationResult(ok=True)
    if not items:
        return result

    def emit(
        event_type: EventType,
        *,
        account_no: str = "",
        symbol: str = "",
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        recorder = event_recorder or record_event
        try:
            recorder(
                event_type,
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                reason=reason,
                payload=payload,
            )
        except Exception:
            # Observability must never block or alter the reconciliation outcome.
            logger.exception("Handoff reconciliation event recorder failed for %s", event_type)

    accounts: Dict[str, List[Any]] = {}
    unassigned: List[Any] = []
    for item in items:
        item_account = str(getattr(item, "kis_account_no", "") or "").strip()
        if item_account:
            accounts.setdefault(item_account, []).append(item)
        else:
            unassigned.append(item)

    emit(
        EventType.RECONCILIATION_STARTED,
        payload={"symbol_count": len(items), "account_count": len(accounts)},
    )

    for item in unassigned:
        symbol = str(getattr(item, "symbol", "") or "").upper()
        result.blocked_symbols.append(symbol)
        result.errors.append(
            f"{symbol or 'unknown symbol'} has no assigned KIS account; manual review required"
        )
        emit(
            EventType.RECONCILIATION_WARNING,
            symbol=symbol,
            reason="missing persisted kis_account_no",
        )

    for item_account, account_items in accounts.items():
        account_symbols = sorted(
            {str(getattr(item, "symbol", "") or "").upper() for item in account_items}
        )

        try:
            discovery = broker.discover_orders(
                environment=environment,
                account_no=item_account,
            )
        except Exception as exc:
            discovery = BrokerOrderDiscoveryResult(errors=[str(exc)])

        if not isinstance(discovery, BrokerOrderDiscoveryResult) or not discovery.complete:
            errors = (
                discovery.errors
                if isinstance(discovery, BrokerOrderDiscoveryResult)
                else ["Broker returned an invalid order-discovery result"]
            )
            detail = "; ".join(errors) or "one or more order sources were incomplete"
            result.blocked_symbols.extend(account_symbols)
            result.errors.append(f"Account {item_account} order discovery incomplete: {detail}")
            emit(
                EventType.RECONCILIATION_FAILED,
                account_no=item_account,
                reason=detail,
            )
            continue

        ambiguous_snapshots = [
            snapshot
            for snapshot in discovery.snapshots
            if str(
                getattr(
                    getattr(snapshot, "status", OrderStatus.UNKNOWN),
                    "value",
                    getattr(snapshot, "status", OrderStatus.UNKNOWN),
                )
            ).upper()
            == OrderStatus.UNKNOWN.value
            or not str(getattr(snapshot, "symbol", "") or "").strip()
            or str(getattr(snapshot, "account_no", "") or "").strip()
            not in {"", item_account}
            or str(getattr(snapshot, "environment", "") or "").upper()
            not in {"", environment.upper()}
        ]
        if ambiguous_snapshots:
            result.blocked_symbols.extend(account_symbols)
            detail = (
                f"{len(ambiguous_snapshots)} unknown or symbol-less broker snapshot(s)"
            )
            result.errors.append(f"Account {item_account} order discovery ambiguous: {detail}")
            emit(
                EventType.RECONCILIATION_FAILED,
                account_no=item_account,
                reason=detail,
            )
            continue

        try:
            positions = broker.get_positions(
                environment=environment,
                account_no=item_account,
            )
        except Exception as exc:
            result.blocked_symbols.extend(account_symbols)
            result.errors.append(f"Account {item_account} position query failed: {exc}")
            emit(
                EventType.RECONCILIATION_FAILED,
                account_no=item_account,
                reason=str(exc),
            )
            continue

        open_orders_by_symbol: Dict[str, List[Any]] = {}
        for snapshot in discovery.snapshots:
            if is_open_status(getattr(snapshot, "status", None)):
                key = str(getattr(snapshot, "symbol", "") or "").upper()
                open_orders_by_symbol.setdefault(key, []).append(snapshot)

        for item in account_items:
            symbol = str(getattr(item, "symbol", "") or "").upper()
            matching_open_orders = open_orders_by_symbol.get(symbol, [])
            if matching_open_orders:
                result.blocked_symbols.append(symbol)
                emit(
                    EventType.RECONCILIATION_WARNING,
                    account_no=item_account,
                    symbol=symbol,
                    reason=f"{len(matching_open_orders)} open broker order(s) found",
                )
                continue

            broker_qty, broker_avg_cost, _ = _holding_for_symbol(positions, symbol)
            local_shares = int(getattr(item, "shares_held", 0) or 0)
            status = str(getattr(item, "monitoring_status", "") or "").upper()

            if status in PRE_ENTRY_QUEUE_STATUSES and broker_qty > 0:
                result.blocked_symbols.append(symbol)
                emit(
                    EventType.RECONCILIATION_WARNING,
                    account_no=item_account,
                    symbol=symbol,
                    reason=(
                        f"pre-entry state but broker already holds {int(broker_qty)} "
                        "share(s); possible fill before state sync"
                    ),
                )
                continue

            if status in POSITION_HOLDING_STATUSES:
                if broker_qty <= 0:
                    result.blocked_symbols.append(symbol)
                    emit(
                        EventType.RECONCILIATION_WARNING,
                        account_no=item_account,
                        symbol=symbol,
                        reason=(
                            f"local shares_held={local_shares} but broker shows no "
                            "position; needs manual review"
                        ),
                    )
                    continue
                local_avg_cost = float(getattr(item, "avg_cost", 0.0) or 0.0)
                if int(broker_qty) != local_shares or (
                    broker_avg_cost > 0
                    and abs(broker_avg_cost - local_avg_cost) > 1e-6
                ):
                    item.shares_held = int(broker_qty)
                    if broker_avg_cost > 0:
                        item.avg_cost = broker_avg_cost
                    emit(
                        EventType.RECONCILIATION_WARNING,
                        account_no=item_account,
                        symbol=symbol,
                        reason="corrected shares_held/avg_cost from broker truth",
                    )

            item._buy_order_pending = False
            item._stop_order_pending = False
            item._exit_order_pending = False
            result.reconciled_symbols.append(symbol)

    result.blocked_symbols = sorted(set(result.blocked_symbols))
    result.reconciled_symbols = sorted(set(result.reconciled_symbols))
    result.ok = not result.blocked_symbols and not result.errors
    emit(
        EventType.RECONCILIATION_COMPLETED if result.ok else EventType.RECONCILIATION_WARNING,
        payload={
            "reconciled": result.reconciled_symbols,
            "blocked": result.blocked_symbols,
        },
    )
    return result
