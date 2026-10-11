"""Account observations and daily market context for the private chat journal."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import io
import json

import pandas as pd

from src.core.journal_metrics import account_day_metrics, finite_number, market_day_move
from src.core.trade_journal import NY_ZONE, parse_time
from src.services.trade_journal import TradeJournalStore, _atomic_text, _json
from src.utils.config import get_env_value
from src.utils.market_calendar import (
    is_nyse_trading_day, nyse_regular_session_close_time, previous_nyse_trading_day,
)

# ETF market-price changes are explicitly distinguished from the underlying index.
MARKET_INSTRUMENTS = (
    {"symbol": "SPY", "name": "S&P 500 — SPY ETF", "unit": "price", "group": "benchmarks"},
    {"symbol": "QQQ", "name": "Nasdaq-100 — QQQ ETF", "unit": "price", "group": "benchmarks"},
    {"symbol": "IWB", "name": "Russell 1000 — IWB ETF", "unit": "price", "group": "benchmarks"},
    {"symbol": "IWM", "name": "Russell 2000 — IWM ETF", "unit": "price", "group": "benchmarks"},
    {"symbol": "RSP", "name": "Equal-weight S&P 500 — RSP ETF", "unit": "price", "group": "benchmarks"},
    {"symbol": "SMH", "name": "Semiconductors — SMH ETF", "unit": "price", "group": "benchmarks"},
    {"symbol": "^VIX", "name": "VIX volatility index", "unit": "index", "group": "macro"},
    {"symbol": "DGS2", "name": "US 2-year Treasury yield", "unit": "yield_percent", "group": "macro"},
    {"symbol": "DGS10", "name": "US 10-year Treasury yield", "unit": "yield_percent", "group": "macro"},
    {"symbol": "TLT", "name": "Long Treasury bond prices — TLT ETF", "unit": "price", "group": "macro"},
    {"symbol": "UUP", "name": "US dollar — UUP fund proxy", "unit": "price", "group": "macro"},
    {"symbol": "GLD", "name": "Gold — GLD trust proxy", "unit": "price", "group": "macro"},
    {"symbol": "USO", "name": "Oil futures — USO fund proxy", "unit": "price", "group": "macro"},
)


def record_journal_nav(snapshot, store: TradeJournalStore | None = None) -> None:
    """Archive existing successful broker reads; never initiate an account fetch.

    Retain the latest regular-session observation and the first read within an
    hour after the close. The latter is an observed NAV, not an official EOD mark.
    """
    moment = snapshot.received_at
    value = finite_number(snapshot.total_equity_usd)
    if (not snapshot.account_no or value is None or value <= 0
            or moment.tzinfo is None):
        return
    local = moment.astimezone(NY_ZONE)
    day = local.date()
    if not is_nyse_trading_day(day):
        return
    opens = dt.datetime.combine(day, dt.time(9, 30), NY_ZONE)
    closes = dt.datetime.combine(day, nyse_regular_session_close_time(day), NY_ZONE)
    if local < opens or local > closes + dt.timedelta(hours=1):
        return
    stage = "post_close" if local >= closes else "intraday"
    account_key = hashlib.sha256(f"{snapshot.environment.upper()}:{snapshot.account_no}".encode()).hexdigest()[:16]
    payload = {"account_key": account_key, "environment": snapshot.environment.upper(),
               "session_date": day.isoformat(), "stage": stage, "nav_usd": value,
               "observed_at": moment.isoformat(), "source": getattr(snapshot, "source", "broker account cache"),
               "basis": "Broker account-equity observation converted to USD; may include FX and non-US assets"}
    store = store or TradeJournalStore()
    with store.connect() as conn:
        if stage == "post_close":
            conn.execute("INSERT OR IGNORE INTO daily_nav VALUES (?,?,?,?)",
                         (account_key, day.isoformat(), stage, _json(payload)))
        else:
            prior = conn.execute("SELECT payload FROM daily_nav WHERE account_key=? AND session_date=? AND stage=?",
                                 (account_key, day.isoformat(), stage)).fetchone()
            if prior and parse_time(json.loads(prior["payload"])["observed_at"]) >= moment:
                return
            conn.execute("INSERT INTO daily_nav VALUES (?,?,?,?) ON CONFLICT(account_key,session_date,stage) "
                         "DO UPDATE SET payload=excluded.payload",
                         (account_key, day.isoformat(), stage, _json(payload)))


def confirm_journal_cash_flow(store: TradeJournalStore, session_date: str, *,
                              account_key: str, net_external_flow_usd: float,
                              confirmation: str) -> None:
    """Record the user's cash-flow facts from chat, including an explicit zero.

    Positive is deposits minus withdrawals between the two NAV observations.
    This confirmation describes cash flows only; it does not approve publishing.
    """
    day = dt.date.fromisoformat(session_date)
    value = finite_number(net_external_flow_usd)
    if value is None or not str(confirmation).strip():
        raise ValueError("A finite net cash flow and the user's confirmation are required")
    previous_day = previous_nyse_trading_day(day - dt.timedelta(days=1)).isoformat()
    with store.connect() as conn:
        observations = {}
        for session in (session_date, previous_day):
            row = conn.execute("SELECT payload FROM daily_nav WHERE account_key=? AND session_date=? AND stage='post_close'",
                               (account_key, session)).fetchone()
            if not row:
                raise ValueError("Both sessions need post-close NAV observations before confirming cash flows")
            observations[session] = json.loads(row["payload"])
        payload = {"net_external_flow_usd": value, "confirmation": str(confirmation).strip(),
                   "source": "user-confirmed in chat", "current_observed_at": observations[session_date]["observed_at"],
                   "previous_observed_at": observations[previous_day]["observed_at"],
                   "confirmed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        conn.execute("INSERT INTO journal_cash_flows VALUES (?,?,?) ON CONFLICT(account_key,session_date) "
                     "DO UPDATE SET payload=excluded.payload", (account_key, session_date, _json(payload)))


def _daily_values(series: pd.Series) -> dict[str, float]:
    """Daily indices label sessions; do not shift UTC midnight to the prior NY day."""
    values, duplicates = {}, set()
    for stamp, raw in series.items():
        try:
            day = pd.Timestamp(stamp).date().isoformat()
        except (ValueError, TypeError):
            continue
        value = finite_number(raw)
        if day in values:
            duplicates.add(day)
        if value is not None:
            values[day] = value
    return {day: value for day, value in values.items() if day not in duplicates}


def fetch_journal_prices(symbols: list[str], day: dt.date) -> dict[str, dict]:
    import yfinance as yf

    previous = previous_nyse_trading_day(day - dt.timedelta(days=1))
    raw = yf.download(symbols, start=previous.isoformat(), end=(day + dt.timedelta(days=1)).isoformat(),
                      interval="1d", auto_adjust=False, back_adjust=False, actions=False,
                      progress=False, threads=8, timeout=8)
    result = {}
    if raw is None or raw.empty:
        return result
    for symbol in symbols:
        frame = raw
        if isinstance(raw.columns, pd.MultiIndex):
            for level in range(raw.columns.nlevels):
                if symbol in raw.columns.get_level_values(level):
                    frame = raw.xs(symbol, axis=1, level=level)
                    break
            else:
                continue
        elif len(symbols) != 1:
            continue
        if "Close" in frame:
            result[symbol] = {"values": _daily_values(frame["Close"]),
                              "source": "Yahoo Finance unadjusted daily close",
                              "source_url": f"https://finance.yahoo.com/quote/{symbol}/history/"}
    return result


def fetch_journal_yields(day: dt.date) -> dict[str, dict]:
    import requests

    previous = previous_nyse_trading_day(day - dt.timedelta(days=1))
    response = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                            params={"id": "DGS2,DGS10", "cosd": previous.isoformat(), "coed": day.isoformat()},
                            timeout=10)
    response.raise_for_status()
    frame = pd.read_csv(io.StringIO(response.text), index_col=0)
    return {symbol: {"values": _daily_values(frame[symbol]), "source": "Federal Reserve H.15 via FRED; percent",
                     "source_url": f"https://fred.stlouisfed.org/series/{symbol}"}
            for symbol in ("DGS2", "DGS10") if symbol in frame}


def sync_journal_market_day(store: TradeJournalStore, session_date: str, *,
                            now: dt.datetime | None = None, force=False,
                            allow_network: bool | None = None, loader=None) -> None:
    """Fill missing close-to-close observations once the session is complete.

    Successful observations are frozen. Missing feeds retry hourly (rates can
    arrive later); retry timestamps are not part of approved journal evidence.
    """
    day = dt.date.fromisoformat(session_date)
    now = now or dt.datetime.now(dt.timezone.utc)
    close = dt.datetime.combine(day, nyse_regular_session_close_time(day), NY_ZONE)
    if not is_nyse_trading_day(day) or now < close + dt.timedelta(minutes=15):
        return
    if allow_network is None:
        allow_network = str(get_env_value("TRADE_JOURNAL_CHART_NETWORK_ENABLED", "true")).lower() in {"true", "1", "yes"}
    if not allow_network:
        return
    with store.connect() as conn:
        saved = conn.execute("SELECT payload FROM journal_market_days WHERE session_date=?", (session_date,)).fetchone()
    payload = json.loads(saved["payload"]) if saved else {"rows": {}}
    attempted = parse_time(payload.get("last_attempt_at"))
    pending = [spec for spec in MARKET_INSTRUMENTS if payload["rows"].get(spec["symbol"], {}).get("change") is None]
    if not pending or (not force and attempted and now - attempted < dt.timedelta(hours=1)):
        return
    payload["last_attempt_at"] = now.isoformat()
    # Save the retry gate before any request so failures cannot flood a provider.
    with store.connect() as conn:
        conn.execute("INSERT INTO journal_market_days VALUES (?,?) ON CONFLICT(session_date) DO UPDATE SET payload=excluded.payload",
                     (session_date, _json(payload)))
    data = {}
    if loader:
        data = loader(day, [spec["symbol"] for spec in pending])
    else:
        prices = [spec["symbol"] for spec in pending if spec["unit"] != "yield_percent"]
        if prices:
            try:
                data.update(fetch_journal_prices(prices, day))
            except Exception:
                pass  # A missing price feed does not suppress the independent rates feed.
        if any(spec["unit"] == "yield_percent" for spec in pending):
            try:
                data.update(fetch_journal_yields(day))
            except Exception:
                pass
    previous = previous_nyse_trading_day(day - dt.timedelta(days=1)).isoformat()
    for spec in pending:
        supplied = data.get(spec["symbol"], {})
        values = dict(supplied.get("values", {}))
        prior_row = payload["rows"].get(spec["symbol"])
        if prior_row and prior_row.get("close") is not None:
            values[session_date] = prior_row["close"]
        move = market_day_move(values.get(session_date), values.get(previous), unit=spec["unit"])
        if move["close"] is None:
            continue
        if prior_row and all(prior_row.get(key) == value for key, value in move.items()):
            continue
        payload["rows"][spec["symbol"]] = {**spec, **move, "session_date": session_date,
            "previous_session_date": previous, "captured_at": now.isoformat(),
            "source": supplied.get("source", "unspecified"), "source_url": supplied.get("source_url", "")}
    with store.connect() as conn:
        conn.execute("UPDATE journal_market_days SET payload=? WHERE session_date=?", (_json(payload), session_date))
    _atomic_text(store.root / "days" / session_date / "market.json", _json(payload["rows"]))


def load_journal_context(store: TradeJournalStore, session_date: str) -> dict:
    day = dt.date.fromisoformat(session_date)
    previous = previous_nyse_trading_day(day - dt.timedelta(days=1)).isoformat()
    accounts = []
    with store.connect() as conn:
        nav_rows = conn.execute("SELECT payload FROM daily_nav WHERE session_date IN (?,?)", (session_date, previous)).fetchall()
        flows = {row["account_key"]: json.loads(row["payload"]) for row in conn.execute(
            "SELECT account_key,payload FROM journal_cash_flows WHERE session_date=?", (session_date,))}
        saved = conn.execute("SELECT payload FROM journal_market_days WHERE session_date=?", (session_date,)).fetchone()
    observations = {}
    for row in nav_rows:
        nav = json.loads(row["payload"])
        key = (nav["account_key"], nav["session_date"])
        if key not in observations or nav["stage"] == "post_close":
            observations[key] = nav
    for (key, session), nav in sorted(observations.items()):
        if session != session_date:
            continue
        prior = observations.get((key, previous))
        accounts.append({"account_key": key, "environment": nav["environment"],
                         "observation": nav, "previous_observation": prior,
                         "cash_flow": flows.get(key), **account_day_metrics(nav, prior, flows.get(key))})
    saved_rows = json.loads(saved["payload"])["rows"] if saved else {}
    rows = [saved_rows.get(spec["symbol"], {**spec, "close": None, "change": None,
                "change_pct": None, "change_bps": None, "status": "Session data unavailable or not yet published"})
            for spec in MARKET_INSTRUMENTS]
    for account in accounts:
        account["excess_return_pp"] = {
            row["symbol"]: account["daily_return_pct"] - row["change_pct"]
            for row in rows if row["symbol"] in {"SPY", "QQQ", "IWB"}
            and account["daily_return_pct"] is not None and row["change_pct"] is not None}
    return {"session_date": session_date, "previous_session_date": previous,
            "accounts": accounts, "market": rows,
            "return_basis": "P&L divided by previous post-close NAV; not time-weighted when cash flows occur. ETF changes use unadjusted market prices, excluding dividends.",
            "macro_note": "Record movements as context. Price changes alone do not establish why a trade won or lost."}


def journal_context_html(context: dict) -> str:
    """Same private performance summary in the desktop and saved journal."""
    body = ["<h3>Daily NAV and P&amp;L (USD)</h3>"]
    if not context["accounts"]:
        body.append("<p>NAV and daily P&amp;L unavailable: no account observation for this session.</p>")
    for index, account in enumerate(context["accounts"], 1):
        nav = account["nav_usd"]
        pnl, ret = account["daily_pnl_usd"], account["daily_return_pct"]
        body.append(f"<p><b>Account {index} ({html.escape(account['environment'])})</b> — observed NAV "
                    f"{'unavailable' if nav is None else f'${nav:,.2f}'}; daily P&amp;L estimate "
                    f"{'pending' if pnl is None else f'${pnl:+,.2f} ({ret:+.2f}%)'}.</p>")
        if account["nav_change_usd"] is not None:
            if account["net_external_flow_usd"] is not None:
                body.append(f"<p>NAV change ${account['nav_change_usd']:+,.2f}; confirmed net external flow ${account['net_external_flow_usd']:+,.2f}.</p>")
            else:
                body.append(f"<p>NAV change ${account['nav_change_usd']:+,.2f}; external cash flows have not been confirmed.</p>")
        body.append(f"<p>{html.escape(account['status'])}. Observed {html.escape(account['observation']['observed_at'])}. "
                    "NAV is the app's account-equity estimate; liabilities and closing marks need broker-statement reconciliation.</p>")
        if account["excess_return_pp"]:
            text = "; ".join(f"{symbol} {value:+.2f} percentage points" for symbol, value in account["excess_return_pp"].items())
            body.append(f"<p>Account return minus benchmark change: {text}.</p>")
    body.append("<h3>Market and macro — daily movements</h3><table cellspacing='6'><tr><th align='left'>Instrument</th><th>Close / level</th><th>Daily change</th></tr>")
    for row in context["market"]:
        close = row["close"]
        level, movement = "unavailable", "unavailable"
        if close is not None:
            level = f"{close:.2f}" + ("%" if row["unit"] == "yield_percent" else "")
            if row["unit"] == "yield_percent" and row["change_bps"] is not None:
                movement = f"{row['change_bps']:+.1f} bps"
            elif row["change_pct"] is not None:
                movement = f"{row['change_pct']:+.2f}%"
                if row["unit"] == "index":
                    movement += f" ({row['change']:+.2f} points)"
        body.append(f"<tr><td>{html.escape(row['name'])}</td><td align='right'>{level}</td><td align='right'>{movement}</td></tr>")
    body.append(f"</table><p>Compared with {context['previous_session_date']}. {html.escape(context['return_basis'])}</p>")
    body.append(f"<p>{html.escape(context['macro_note'])}</p>")
    return "".join(body)
