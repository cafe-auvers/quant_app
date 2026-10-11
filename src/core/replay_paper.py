"""Deterministic, long-only paper ledger built from manual actions and cached bars."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_FLOOR


def amount(value):
    return Decimal(str(value))


def buy_plan(cash, nav, price, stop, quantity=0, average=0, risk_pct=1):
    cash, nav, price, stop, average = map(amount, (cash, nav, price, stop, average))
    if price <= 0 or not 0 < stop < price:
        raise ValueError("Stop must be below the buy price and above zero")
    budget = nav * amount(risk_pct) / 100
    used = amount(quantity) * max(average - stop, Decimal(0))
    remaining = max(Decimal(0), budget - used)
    shares = int(min(cash / price, remaining / (price - stop)).to_integral_value(rounding=ROUND_FLOOR))
    return {"shares": max(0, shares), "risk_budget": float(budget),
        "risk_amount": float(max(0, shares) * (price - stop)), "notional": float(max(0, shares) * price)}


def paper_ledger(actions, dates, load_bars, through, initial_cash=10000, risk_pct=1):
    """A missing session stops reconciliation; an OHLC same-bar stop is conservative."""
    cash = amount(initial_cash)
    quantity, average, stop, realized = 0, Decimal(0), None, Decimal(0)
    mark, mark_time, pending = None, None, None
    fills, curve, warnings = [], [], []
    grouped = {}
    for action in sorted(actions, key=lambda a: (a["time"], a.get("sequence", 0), a["id"])):
        if action["day"] <= through:
            grouped.setdefault(action["day"], []).append(action)

    def fill(action_id, stamp, day, side, shares, price, reason, source, detail="", risk_budget=None):
        nonlocal cash, quantity, average, stop, realized
        price = amount(price)
        pnl = Decimal(0)
        position_stop = stop
        if side == "buy":
            total = quantity + shares
            average = (average * quantity + price * shares) / total
            cash -= price * shares
            quantity = total
        else:
            pnl = (price - average) * shares
            realized += pnl
            cash += price * shares
            quantity -= shares
            if not quantity:
                average, stop = Decimal(0), None
        fills.append({"id": action_id, "time": stamp, "day": day.isoformat(), "side": side,
            "shares": shares, "price": float(price), "reason": reason, "source": source,
            "detail": detail, "realized_pnl": float(pnl), "cash_after": float(cash),
            "nav_after": float(cash + quantity * price), "risk_budget": risk_budget,
            "stop_price": float(position_stop) if position_stop is not None else None})

    def touch_stop(bar, day, source, anchor, same_bar=False, entry_price=None):
        if not quantity or stop is None or amount(bar["low"]) > stop:
            return
        price = min(stop, amount(bar["open"])) if not same_bar else stop
        uncertain = same_bar and amount(bar["open"]) != amount(entry_price)
        reason = "stop_same_bar" if uncertain else "stop_gap" if price < stop else "stop_loss"
        detail = "Entry/stop order inside this candle is unknown; conservative stop assumed." if uncertain else ""
        if detail:
            warnings.append(detail)
        fill(f"stop:{anchor}:{bar['time']}", bar["time"], day, "sell", quantity, price, reason, source, detail)

    anchor = None
    for day in dates:
        if day > through:
            break
        day_actions = grouped.get(day, [])
        if not quantity and not day_actions:
            continue
        packet = load_bars(day)
        bars = packet.get("bars", [])
        if not bars:
            pending = {"day": day.isoformat(), "reason": packet.get("reason", "Five-minute data unavailable")}
            break
        source = packet["source"]
        stamps = {bar["time"] for bar in bars}
        if any(action["time"] not in stamps for action in day_actions):
            raise ValueError("Trade time must match an available five-minute candle")
        if not packet.get("complete", False):
            warnings.append(f"{day}: incomplete five-minute coverage; stop results use available bars.")
        at_time = {}
        for action in day_actions:
            at_time.setdefault(action["time"], []).append(action)
        for bar in bars:
            touch_stop(bar, day, source, anchor)
            for action in at_time.get(bar["time"], []):
                price = amount(action["price"])
                if not amount(bar["low"]) <= price <= amount(bar["high"]):
                    raise ValueError("Fill price must be within the selected five-minute candle's range")
                if action["action"] == "buy":
                    proposed_stop = amount(action["stop"])
                    if quantity and abs(proposed_stop - stop) > Decimal("0.000001"):
                        raise ValueError("Additional buys must keep the existing position's stop price")
                    plan = buy_plan(cash, cash + quantity * price, price, proposed_stop, quantity, average, risk_pct)
                    shares = action.get("shares") or plan["shares"]
                    if shares < 1:
                        raise ValueError("No shares fit the remaining 1% risk budget and available cash")
                    if shares * price > cash or shares > plan["shares"]:
                        raise ValueError("Buy exceeds available cash or the position's risk budget")
                    stop = proposed_stop
                    anchor = anchor if quantity else action["id"]
                    fill(action["id"], bar["time"], day, "buy", shares, price, "manual_buy", source,
                        risk_budget=plan["risk_budget"])
                    touch_stop(bar, day, source, anchor, same_bar=True, entry_price=price)
                else:
                    if not quantity:
                        raise ValueError("There is no open position at this candle; check earlier stop-loss fills")
                    shares = action.get("shares") or (quantity if action["action"] == "sell_all" else
                        max(1, int(quantity * amount(action.get("percent", 50)) / 100)))
                    if not 0 < shares <= quantity:
                        raise ValueError("Sell quantity exceeds the open position")
                    fill(action["id"], bar["time"], day, "sell", shares, price, action["action"], source)
            mark, mark_time = amount(bar["close"]), bar["time"]
        curve.append({"day": day.isoformat(), "nav": float(cash + quantity * mark), "cash": float(cash)})
    valuation = mark or average
    return {"initial_cash": float(initial_cash), "risk_pct": risk_pct, "cash": float(cash),
        "quantity": quantity, "average_price": float(average) if quantity else None,
        "stop_price": float(stop) if stop is not None else None,
        "market_price": float(valuation) if quantity else None, "mark_time": mark_time,
        "nav": float(cash + quantity * valuation), "realized_pnl": float(realized),
        "unrealized_pnl": float(quantity * (valuation - average)),
        "return_pct": float((cash + quantity * valuation) / amount(initial_cash) * 100 - 100),
        "risk_budget": float((cash + quantity * valuation) * amount(risk_pct) / 100),
        "fills": fills, "equity_curve": curve, "warnings": list(dict.fromkeys(warnings)),
        "pending": pending, "asof": through.isoformat(), "scope": "Per stock · $10,000 start · 1% NAV risk"}
