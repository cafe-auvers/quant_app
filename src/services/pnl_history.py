"""Daily P&L history for the Health tab dashboard.

Builds a date-indexed equity curve from two sources:

- **Realized P&L** uses KIS's actual period-profit rows where available, so
  externally placed trades and broker costs are included. The full local
  order ledger is FIFO-matched as a fallback outside KIS coverage.
- **Unrealized P&L** is a mark-to-market snapshot of currently open buylist
  positions against the latest known price. There is no historical price
  history stored per position, so this can only be captured going forward
  from whenever a snapshot is first taken — past days show 0 unrealized
  (i.e. realized-only) until then.

Snapshots are keyed by US market session date (one row per trading day) and
merged/persisted idempotently. Fresh KIS rows replace, rather than add to, the
same account/date's local reconstruction. Past unrealized/FX/capital-base
values are preserved and only today's values are overwritten.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Tuple

from src.core.exit_policy import market_session_date, market_session_date_from_value
from src.core.order_state import BrokerOrder, OrderSide
from src.utils.config import DATA_DIR
from src.utils.storage import load_json, save_json

PNL_HISTORY_FILE = DATA_DIR / "pnl_history.json"
logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class BrokerRealizedPnlSeries:
    """Authoritative broker realized P/L coverage for one account."""

    account_no: str
    start_date: str
    end_date: str
    daily_usd: Mapping[str, float]


@dataclass
class PnlDailySnapshot:
    """One day's point on the P&L equity curve.

    ``realized_usd``, ``unrealized_usd`` and ``total_usd`` are all *cumulative
    as of end of this day*, not a delta for the day, so the series can be
    plotted directly as a running equity curve.
    """

    date: str
    realized_usd: float = 0.0
    unrealized_usd: float = 0.0
    total_usd: float = 0.0
    fx_rate: Optional[float] = None  # KRW per USD in effect that day
    capital_base_usd: Optional[float] = None  # account size used for % conversion
    computed_at: str = ""
    realized_source: str = "local order history"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PnlDailySnapshot":
        def _optional_float(value: Any) -> Optional[float]:
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return cls(
            date=str(data.get("date", "")),
            realized_usd=float(data.get("realized_usd", 0.0) or 0.0),
            unrealized_usd=float(data.get("unrealized_usd", 0.0) or 0.0),
            total_usd=float(data.get("total_usd", 0.0) or 0.0),
            fx_rate=_optional_float(data.get("fx_rate")),
            capital_base_usd=_optional_float(data.get("capital_base_usd")),
            computed_at=str(data.get("computed_at", "")),
            realized_source=str(
                data.get("realized_source") or "local order history"
            ),
        )


def _normalized_account_no(value: Any) -> str:
    return "".join(char for char in str(value or "") if char.isalnum()).upper()


def _compute_ledger_realized_pnl_by_account_date(
    orders: Iterable[BrokerOrder],
) -> Dict[Tuple[str, str], float]:
    """FIFO-match the local ledger, retaining account identity for overrides."""
    fills = [
        order
        for order in orders
        if str(order.environment).upper() == "PROD" and order.filled_quantity > 0
    ]
    fills.sort(key=lambda order: order.updated_at or order.submitted_at or "")

    open_lots: Dict[Tuple[str, str, str], Deque[List[float]]] = defaultdict(deque)
    daily_delta: Dict[Tuple[str, str], float] = defaultdict(float)

    for order in fills:
        account_no = _normalized_account_no(order.account_no)
        key = (str(order.environment).upper(), account_no, order.symbol)
        quantity = float(order.filled_quantity)
        price = float(order.avg_fill_price)
        session_date = market_session_date_from_value(
            order.updated_at or order.submitted_at
        )
        if order.side == OrderSide.BUY:
            if price > 0 and quantity > 0:
                open_lots[key].append([quantity, price])
            continue
        if order.side != OrderSide.SELL:
            continue

        remaining = quantity
        realized = 0.0
        lots = open_lots[key]
        while remaining > 1e-9 and lots:
            lot_quantity, lot_price = lots[0]
            matched = min(lot_quantity, remaining)
            realized += (price - lot_price) * matched
            lot_quantity -= matched
            remaining -= matched
            if lot_quantity <= 1e-9:
                lots.popleft()
            else:
                lots[0][0] = lot_quantity
        if session_date is not None and abs(realized) > 1e-9:
            daily_delta[(account_no, str(session_date))] += realized

    return dict(daily_delta)


def compute_realized_pnl_by_date(
    orders: Iterable[BrokerOrder],
    *,
    broker_realized_series: Optional[Iterable[BrokerRealizedPnlSeries]] = None,
) -> Dict[str, float]:
    """FIFO-match BUY lots to SELL fills per (environment, account_no, symbol).

    Returns realized P&L in USD *for that day only* (a delta, not cumulative),
    keyed by US market session date "YYYY-MM-DD". Only PROD orders count —
    the buylist/trading path only ever runs live orders in that environment.

    Each ledger row is treated as one fill event at its full filled_quantity /
    avg_fill_price, bucketed by its updated_at (falling back to submitted_at).
    The ledger stores order-level running averages, not per-fill timestamps,
    so this is the finest granularity available.

    A SELL that exceeds the tracked open lots (e.g. a position opened before
    the ledger existed) can only realize P&L for the portion with a known
    cost basis; the untracked remainder is silently excluded rather than
    guessed at. When KIS period-profit data is supplied for an account/date
    range, it is authoritative for that covered account and replaces (rather
    than adds to) the local reconstruction, preventing double-counting.
    """
    ledger_daily = _compute_ledger_realized_pnl_by_account_date(orders)
    broker_by_account: Dict[str, BrokerRealizedPnlSeries] = {}
    for series in broker_realized_series or ():
        account_no = _normalized_account_no(series.account_no)
        if account_no and series.start_date <= series.end_date:
            broker_by_account[account_no] = series

    daily_delta: Dict[str, float] = defaultdict(float)
    for (account_no, session_date), value in ledger_daily.items():
        broker = broker_by_account.get(account_no)
        if broker and broker.start_date <= session_date <= broker.end_date:
            continue
        daily_delta[session_date] += value

    for account_no, series in broker_by_account.items():
        for raw_date, raw_value in series.daily_usd.items():
            session_date = str(raw_date)
            if not (series.start_date <= session_date <= series.end_date):
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            daily_delta[session_date] += value

    return dict(daily_delta)


def compute_unrealized_pnl_usd(
    positions: Iterable[Mapping[str, Any]], prices: Mapping[str, float]
) -> float:
    """Mark-to-market P&L across currently open positions.

    ``positions`` is a plain-dict view (symbol/shares_held/avg_cost) so this
    stays independent of any Qt/BuylistItem import — callers gather that view
    from ``buylist_manager.items`` on the UI side.
    """
    total = 0.0
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        shares_held = float(position.get("shares_held", 0) or 0)
        avg_cost = float(position.get("avg_cost", 0) or 0)
        price = float(prices.get(symbol, 0.0) or 0.0)
        if shares_held > 0 and avg_cost > 0 and price > 0:
            total += (price - avg_cost) * shares_held
    return total


def build_pnl_history(
    orders: Iterable[BrokerOrder],
    *,
    today: str,
    unrealized_usd_today: float,
    fx_rate_today: Optional[float],
    capital_base_usd_today: Optional[float],
    existing: Optional[Iterable[PnlDailySnapshot]] = None,
    broker_realized_series: Optional[Iterable[BrokerRealizedPnlSeries]] = None,
) -> List[PnlDailySnapshot]:
    """Merge a freshly-derived realized curve with previously stored days.

    Realized P/L prefers supplied broker series and uses ``orders`` only as a
    fallback. Unrealized/FX/capital-base are read through from the previous
    snapshot for every day except ``today``, which takes fresh live values.
    """
    existing_by_date = {snap.date: snap for snap in (existing or [])}
    broker_realized_series = tuple(broker_realized_series or ())
    daily_delta = compute_realized_pnl_by_date(
        orders, broker_realized_series=broker_realized_series
    )
    if broker_realized_series:
        realized_source = "KIS actual period P/L (local fallback outside coverage)"
    else:
        # Opening Health can race the asynchronous startup KIS preload. Do not
        # replace previously fetched broker truth with a partial local-ledger
        # reconstruction during that window. Preserve the stored actual curve
        # through its last known date, then allow newer ledger deltas as a
        # clearly labelled temporary fallback until KIS refreshes again.
        stored_actual = sorted(
            (
                snap
                for snap in existing_by_date.values()
                if snap.realized_source.startswith("KIS actual")
                or snap.realized_source.startswith("stored KIS actual")
            ),
            key=lambda snap: snap.date,
        )
        if stored_actual:
            last_stored_date = stored_actual[-1].date
            prior_realized = 0.0
            stored_daily: Dict[str, float] = {}
            for snapshot in stored_actual:
                stored_daily[snapshot.date] = (
                    snapshot.realized_usd - prior_realized
                )
                prior_realized = snapshot.realized_usd
            daily_delta = {
                date: value
                for date, value in daily_delta.items()
                if date > last_stored_date
            }
            daily_delta.update(stored_daily)
            realized_source = "stored KIS actual P/L (live refresh pending)"
        else:
            realized_source = "local order history"

    all_dates = sorted(set(daily_delta) | set(existing_by_date) | {today})

    results: List[PnlDailySnapshot] = []
    running_realized = 0.0
    last_fx: Optional[float] = None
    last_capital_base: Optional[float] = None

    for date in all_dates:
        running_realized += daily_delta.get(date, 0.0)
        prior = existing_by_date.get(date)

        if date == today:
            unrealized = unrealized_usd_today
            fx_rate = fx_rate_today if fx_rate_today is not None else (
                prior.fx_rate if prior else last_fx
            )
            capital_base = (
                capital_base_usd_today
                if capital_base_usd_today is not None
                else (prior.capital_base_usd if prior else last_capital_base)
            )
            computed_at = _utc_now_iso()
        else:
            unrealized = prior.unrealized_usd if prior else 0.0
            fx_rate = prior.fx_rate if prior else last_fx
            capital_base = prior.capital_base_usd if prior else last_capital_base
            computed_at = prior.computed_at if prior else _utc_now_iso()

        if fx_rate is not None:
            last_fx = fx_rate
        if capital_base is not None:
            last_capital_base = capital_base

        results.append(
            PnlDailySnapshot(
                date=date,
                realized_usd=running_realized,
                unrealized_usd=unrealized,
                total_usd=running_realized + unrealized,
                fx_rate=fx_rate,
                capital_base_usd=capital_base,
                computed_at=computed_at,
                realized_source=realized_source,
            )
        )

    return results


def load_pnl_history(path: Path = PNL_HISTORY_FILE) -> List[PnlDailySnapshot]:
    data = load_json(path, default={"snapshots": []})
    raw_snapshots = data.get("snapshots")
    if not isinstance(raw_snapshots, list):
        return []
    snapshots = []
    for entry in raw_snapshots:
        if not isinstance(entry, dict):
            continue
        try:
            snapshots.append(PnlDailySnapshot.from_dict(entry))
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping unreadable P&L history row: %s", exc)
    snapshots.sort(key=lambda snap: snap.date)
    return snapshots


def save_pnl_history(
    snapshots: Iterable[PnlDailySnapshot], path: Path = PNL_HISTORY_FILE
) -> None:
    save_json(
        path,
        {"snapshots": [snap.to_dict() for snap in snapshots]},
    )


def record_daily_pnl_snapshot(
    orders: Iterable[BrokerOrder],
    *,
    unrealized_usd_today: float,
    fx_rate_today: Optional[float],
    capital_base_usd_today: Optional[float],
    broker_realized_series: Optional[Iterable[BrokerRealizedPnlSeries]] = None,
    today: Optional[str] = None,
    path: Path = PNL_HISTORY_FILE,
) -> List[PnlDailySnapshot]:
    """Recompute the full history, merge in today's live values, and persist it."""
    today = today or str(market_session_date())
    existing = load_pnl_history(path)
    updated = build_pnl_history(
        orders,
        today=today,
        unrealized_usd_today=unrealized_usd_today,
        fx_rate_today=fx_rate_today,
        capital_base_usd_today=capital_base_usd_today,
        existing=existing,
        broker_realized_series=broker_realized_series,
    )
    save_pnl_history(updated, path)
    return updated
