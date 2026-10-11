"""Daily account and benchmark arithmetic; unknown values stay unknown."""
from __future__ import annotations

import math


def finite_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def account_day_metrics(current: dict | None, previous: dict | None,
                        cash_flow: dict | None) -> dict:
    """Compare two post-close observations after confirmed external flows."""
    result = {"nav_usd": None, "previous_nav_usd": None, "nav_change_usd": None,
              "net_external_flow_usd": None, "daily_pnl_usd": None,
              "daily_return_pct": None, "status": "NAV unavailable"}
    if not current:
        return result
    nav = finite_number(current.get("nav_usd"))
    if nav is None or nav <= 0:
        return result
    result["nav_usd"] = nav
    result["status"] = "Intraday NAV; post-close observation pending"
    if current.get("stage") != "post_close":
        return result
    base = finite_number(previous.get("nav_usd")) if previous else None
    if base is None or base <= 0 or previous.get("stage") != "post_close":
        result["status"] = "Previous session post-close NAV unavailable"
        return result
    result["previous_nav_usd"] = base
    result["nav_change_usd"] = nav - base
    flow = finite_number(cash_flow.get("net_external_flow_usd")) if cash_flow else None
    if flow is None or not cash_flow.get("confirmation"):
        result["status"] = "NAV change recorded; external cash flows need confirmation"
        return result
    # A cash-flow review applies to these exact observations, not later revisions.
    if (cash_flow.get("current_observed_at") != current.get("observed_at")
            or cash_flow.get("previous_observed_at") != previous.get("observed_at")):
        result["status"] = "NAV observations changed; review external cash flows again"
        return result
    pnl = nav - base - flow
    result.update(net_external_flow_usd=flow, daily_pnl_usd=pnl,
                  daily_return_pct=pnl / base * 100,
                  status="Cash-flow-adjusted account-equity estimate between post-close observations")
    return result


def market_day_move(current, previous, *, unit: str = "price") -> dict:
    current, previous = finite_number(current), finite_number(previous)
    if unit != "yield_percent":
        current = current if current is not None and current > 0 else None
        previous = previous if previous is not None and previous > 0 else None
    result = {"close": current, "previous_close": previous,
              "change": None, "change_pct": None, "change_bps": None}
    if current is None or previous is None:
        return result
    result["change"] = current - previous
    if unit == "yield_percent":
        result["change_bps"] = (current - previous) * 100
    else:
        result["change_pct"] = (current / previous - 1) * 100
    return result
