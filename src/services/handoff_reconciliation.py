"""Pre-resume broker reconciliation for cross-machine main-device handoff.

The single most safety-critical piece of the automatic laptop<->PC handoff
feature. Before a newly-main device resumes monitoring/auto-submission, it
must confirm -- against the broker directly, never against synced local
state -- that every configured PROD account is free of unexplained exposure.

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
only after the entire assigned account's broker snapshot is unambiguous.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.api.kis_account_snapshot_dual import (
    KisEnvironment,
    get_configured_account_numbers,
)
from src.core.execution_queue import (
    HANDOFF_MONITORABLE_STATUSES,
    POSITION_HOLDING_STATUSES,
    PRE_ENTRY_QUEUE_STATUSES,
)
from src.core.order_state import BrokerOrderDiscoveryResult, OrderStatus, is_open_status
from src.services.broker import Broker, KisBroker
from src.services.event_journal import EventType, record_event

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
    environment_key = str(environment or "").strip().upper()
    return [
        item
        for item in getattr(buylist_manager, "items", [])
        if str(getattr(item, "environment", "") or "").upper() == environment_key
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


def _nonzero_holdings_by_symbol(
    snapshot: Any,
) -> Tuple[Dict[str, Tuple[float, float]], List[str]]:
    """Validate a full position snapshot and return every nonzero exposure.

    Handoff reconciliation requests both domestic and overseas balances. A
    missing/malformed section cannot safely mean "no holdings", so this
    parser reports it as an error instead of silently returning an empty map.
    """
    exposures: Dict[str, Tuple[float, float]] = {}
    errors: List[str] = []
    if not isinstance(snapshot, dict):
        return exposures, ["broker returned an invalid position snapshot"]

    for section_name in ("domestic", "overseas"):
        section = snapshot.get(section_name)
        if not isinstance(section, dict):
            errors.append(f"position snapshot is missing {section_name}")
            continue
        holdings = section.get("holdings")
        if not isinstance(holdings, list):
            errors.append(f"{section_name} position holdings are invalid")
            continue
        for index, holding in enumerate(holdings):
            if not isinstance(holding, dict):
                errors.append(
                    f"{section_name} holding row {index + 1} is invalid"
                )
                continue
            symbol = str(holding.get("symbol", "") or "").strip().upper()
            try:
                quantity = float(holding.get("quantity", 0) or 0)
                average_price = float(holding.get("average_price", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                errors.append(
                    f"{section_name} holding row {index + 1} has invalid numbers"
                )
                continue
            if not math.isfinite(quantity) or not math.isfinite(average_price):
                errors.append(
                    f"{section_name} holding row {index + 1} has non-finite numbers"
                )
                continue
            if quantity == 0:
                continue
            if not symbol:
                errors.append(
                    f"{section_name} contains a nonzero symbol-less holding"
                )
                continue
            if symbol in exposures:
                errors.append(
                    f"position snapshot contains duplicate nonzero rows for {symbol}"
                )
                continue
            exposures[symbol] = (quantity, average_price)

    return exposures, errors


def run_post_claim_broker_reconciliation(
    buylist_manager,
    *,
    environment: str = "PROD",
    broker: Optional[Broker] = None,
    event_recorder: Optional[Callable[..., Any]] = None,
    configured_account_numbers: Optional[Sequence[str]] = None,
) -> PostClaimReconciliationResult:
    """Reconcile every configured account and in-flight item against broker truth.

    Uses broker-truth discovery, not the local order ledger. Every configured
    account is queried independently of synchronized buylist contents, which
    prevents a stale/failed publication from hiding an order or position.
    Item-assigned accounts are also queried, but an assignment that is absent
    from local PROD configuration fails closed.

    An account only clears when both order and position snapshots are
    complete and every broker-side exposure is represented by synchronized
    authoritative state. Any open order, unmatched nonzero holding,
    disagreement, malformed response, or API error keeps all of that
    account's local runtime flags locked. The caller must not start the
    monitor or arm trading unless the overall result is clean.
    """
    broker = KisBroker() if broker is None else broker
    environment_key = str(environment or "").strip().upper()
    items = in_flight_buylist_items(buylist_manager, environment=environment)
    result = PostClaimReconciliationResult(ok=True)

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

    account_config_error = ""
    if configured_account_numbers is None:
        try:
            configured_account_numbers = get_configured_account_numbers(
                KisEnvironment(environment_key)
            )
        except Exception as exc:
            configured_account_numbers = []
            account_config_error = str(exc)

    configured_accounts: List[str] = []
    seen_accounts = set()
    for raw_account in configured_account_numbers:
        account_no = str(raw_account or "").strip()
        if account_no and account_no not in seen_accounts:
            configured_accounts.append(account_no)
            seen_accounts.add(account_no)

    accounts: Dict[str, List[Any]] = {
        account_no: [] for account_no in configured_accounts
    }
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

    if account_config_error:
        result.errors.append(
            f"Could not enumerate configured {environment_key} KIS accounts: "
            f"{account_config_error}"
        )
        emit(
            EventType.RECONCILIATION_FAILED,
            reason=result.errors[-1],
        )
    if not configured_accounts:
        result.errors.append(
            f"No configured {environment_key} KIS accounts; account-wide "
            "handoff reconciliation cannot prove broker exposure is absent"
        )
        emit(
            EventType.RECONCILIATION_FAILED,
            reason=result.errors[-1],
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
        account_failed = False
        corrections: List[Tuple[Any, int, float, str]] = []

        if item_account not in seen_accounts:
            account_failed = True
            result.errors.append(
                f"Account {item_account} is assigned to synchronized in-flight "
                f"state but is not configured for {environment_key}"
            )
            emit(
                EventType.RECONCILIATION_FAILED,
                account_no=item_account,
                reason="account is not in configured account inventory",
            )

        try:
            discovery = broker.discover_orders(
                environment=environment,
                account_no=item_account,
            )
        except Exception as exc:
            discovery = BrokerOrderDiscoveryResult(errors=[str(exc)])

        discovery_valid = isinstance(discovery, BrokerOrderDiscoveryResult)
        snapshots = list(discovery.snapshots) if discovery_valid else []
        if not isinstance(discovery, BrokerOrderDiscoveryResult) or not discovery.complete:
            errors = (
                discovery.errors
                if isinstance(discovery, BrokerOrderDiscoveryResult)
                else ["Broker returned an invalid order-discovery result"]
            )
            detail = "; ".join(errors) or "one or more order sources were incomplete"
            account_failed = True
            result.blocked_symbols.extend(account_symbols)
            result.errors.append(f"Account {item_account} order discovery incomplete: {detail}")
            emit(
                EventType.RECONCILIATION_FAILED,
                account_no=item_account,
                reason=detail,
            )

        ambiguous_snapshots = [
            snapshot
            for snapshot in snapshots
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
            account_failed = True
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
        ambiguous_snapshot_ids = {id(snapshot) for snapshot in ambiguous_snapshots}

        positions = None
        positions_query_succeeded = False
        try:
            positions = broker.get_positions(
                environment=environment,
                account_no=item_account,
            )
            positions_query_succeeded = True
        except Exception as exc:
            account_failed = True
            result.blocked_symbols.extend(account_symbols)
            result.errors.append(f"Account {item_account} position query failed: {exc}")
            emit(
                EventType.RECONCILIATION_FAILED,
                account_no=item_account,
                reason=str(exc),
            )

        holdings_by_symbol: Dict[str, Tuple[float, float]] = {}
        if positions_query_succeeded:
            holdings_by_symbol, position_errors = _nonzero_holdings_by_symbol(positions)
            if position_errors:
                account_failed = True
                result.blocked_symbols.extend(account_symbols)
                detail = "; ".join(position_errors)
                result.errors.append(
                    f"Account {item_account} position discovery ambiguous: {detail}"
                )
                emit(
                    EventType.RECONCILIATION_FAILED,
                    account_no=item_account,
                    reason=detail,
                )

        open_orders_by_symbol: Dict[str, List[Any]] = {}
        for snapshot in snapshots:
            if id(snapshot) in ambiguous_snapshot_ids:
                continue
            if is_open_status(getattr(snapshot, "status", None)):
                key = str(getattr(snapshot, "symbol", "") or "").upper()
                open_orders_by_symbol.setdefault(key, []).append(snapshot)

        represented_order_symbols = set(account_symbols)
        for symbol, matching_orders in open_orders_by_symbol.items():
            account_failed = True
            result.blocked_symbols.append(symbol)
            if symbol not in represented_order_symbols:
                result.errors.append(
                    f"Account {item_account} has {len(matching_orders)} unmatched "
                    f"open broker order(s) for {symbol}"
                )
                reason = "open broker order is absent from synchronized in-flight state"
            else:
                reason = f"{len(matching_orders)} open broker order(s) found"
            emit(
                EventType.RECONCILIATION_WARNING,
                account_no=item_account,
                symbol=symbol,
                reason=reason,
            )

        represented_holding_symbols = {
            str(getattr(item, "symbol", "") or "").upper()
            for item in account_items
            if str(getattr(item, "monitoring_status", "") or "").upper()
            in POSITION_HOLDING_STATUSES
        }
        for symbol, (broker_qty, _broker_avg_cost) in holdings_by_symbol.items():
            if symbol in represented_holding_symbols:
                continue
            account_failed = True
            result.blocked_symbols.append(symbol)
            result.errors.append(
                f"Account {item_account} has unmatched broker holding {symbol} "
                f"with quantity {broker_qty:g}; no synchronized BOUGHT item represents it"
            )
            emit(
                EventType.RECONCILIATION_WARNING,
                account_no=item_account,
                symbol=symbol,
                reason="nonzero broker holding is absent from synchronized BOUGHT state",
            )

        for item in account_items:
            symbol = str(getattr(item, "symbol", "") or "").upper()
            matching_open_orders = open_orders_by_symbol.get(symbol, [])
            if matching_open_orders:
                continue

            broker_qty, broker_avg_cost = holdings_by_symbol.get(symbol, (0.0, 0.0))
            local_shares = int(getattr(item, "shares_held", 0) or 0)
            status = str(getattr(item, "monitoring_status", "") or "").upper()

            if status in PRE_ENTRY_QUEUE_STATUSES and broker_qty != 0:
                account_failed = True
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
                    account_failed = True
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
                    corrections.append((item, int(broker_qty), broker_avg_cost, symbol))

        if account_failed:
            result.blocked_symbols.extend(account_symbols)
            continue

        for item, broker_qty, broker_avg_cost, symbol in corrections:
            item.shares_held = broker_qty
            if broker_avg_cost > 0:
                item.avg_cost = broker_avg_cost
            emit(
                EventType.RECONCILIATION_WARNING,
                account_no=item_account,
                symbol=symbol,
                reason="corrected shares_held/avg_cost from broker truth",
            )

        for item in account_items:
            symbol = str(getattr(item, "symbol", "") or "").upper()
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
