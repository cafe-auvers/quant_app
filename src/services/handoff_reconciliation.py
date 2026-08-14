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
)
from src.core.order_state import is_open_status
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
    account_no: str = "",
    broker: Optional[Broker] = None,
    event_recorder: Optional[Callable[..., Any]] = None,
) -> PostClaimReconciliationResult:
    """Reconcile every in-flight PROD item against broker truth.

    Uses broker-truth discovery, not the local order ledger:
    ``broker.get_order(..., symbol="")`` queries KIS's account-wide
    open-order/history endpoints (``inquire-nccs``/``inquire-ccnl`` via
    ``src/api/kis_order.py``'s ``query_overseas_order``) rather than being
    scoped to any local ledger, so this can see an order the *other* device
    placed that this device's own ``orders.json`` has never heard of.

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

    def emit(event_type: EventType, *, symbol: str = "", reason: str = "", payload: Optional[Dict[str, Any]] = None) -> None:
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

    emit(EventType.RECONCILIATION_STARTED, payload={"symbol_count": len(items)})

    try:
        open_order_snapshots = broker.get_order(
            environment=environment, account_no=account_no, symbol=""
        )
    except Exception as exc:
        result.ok = False
        result.errors.append(f"Account-wide open-order query failed: {exc}")
        result.blocked_symbols = sorted({str(item.symbol or "").upper() for item in items})
        emit(EventType.RECONCILIATION_FAILED, reason=str(exc))
        return result

    try:
        positions = broker.get_positions(environment=environment, account_no=account_no)
    except Exception as exc:
        result.ok = False
        result.errors.append(f"Position query failed: {exc}")
        result.blocked_symbols = sorted({str(item.symbol or "").upper() for item in items})
        emit(EventType.RECONCILIATION_FAILED, reason=str(exc))
        return result

    open_orders_by_symbol: Dict[str, List[Any]] = {}
    for snapshot in open_order_snapshots:
        if is_open_status(getattr(snapshot, "status", None)):
            key = str(getattr(snapshot, "symbol", "") or "").upper()
            open_orders_by_symbol.setdefault(key, []).append(snapshot)

    for item in items:
        symbol = str(item.symbol or "").upper()
        matching_open_orders = open_orders_by_symbol.get(symbol, [])
        if matching_open_orders:
            result.blocked_symbols.append(symbol)
            emit(
                EventType.RECONCILIATION_WARNING,
                symbol=symbol,
                reason=f"{len(matching_open_orders)} open broker order(s) found",
            )
            continue

        broker_qty, broker_avg_cost, _ = _holding_for_symbol(positions, symbol)
        local_shares = int(getattr(item, "shares_held", 0) or 0)
        status = str(getattr(item, "monitoring_status", "") or "").upper()

        if status in POSITION_HOLDING_STATUSES:
            if broker_qty <= 0:
                # Unambiguous disagreement: local state says BOUGHT but the
                # broker shows no position (e.g. a sell filled on the other
                # device that this one never heard about). Stay blocked for
                # manual review rather than silently dropping the position.
                result.blocked_symbols.append(symbol)
                emit(
                    EventType.RECONCILIATION_WARNING,
                    symbol=symbol,
                    reason=(
                        f"local shares_held={local_shares} but broker shows no "
                        "position; needs manual review"
                    ),
                )
                continue
            local_avg_cost = float(getattr(item, "avg_cost", 0.0) or 0.0)
            if int(broker_qty) != local_shares or (
                broker_avg_cost > 0 and abs(broker_avg_cost - local_avg_cost) > 1e-6
            ):
                item.shares_held = int(broker_qty)
                if broker_avg_cost > 0:
                    item.avg_cost = broker_avg_cost
                emit(
                    EventType.RECONCILIATION_WARNING,
                    symbol=symbol,
                    reason="corrected shares_held/avg_cost from broker truth",
                )

        item._buy_order_pending = False
        item._stop_order_pending = False
        item._exit_order_pending = False
        result.reconciled_symbols.append(symbol)

    result.ok = not result.blocked_symbols
    emit(
        EventType.RECONCILIATION_COMPLETED if result.ok else EventType.RECONCILIATION_WARNING,
        payload={
            "reconciled": result.reconciled_symbols,
            "blocked": result.blocked_symbols,
        },
    )
    return result
