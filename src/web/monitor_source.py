"""Read monitoring membership and position inputs without contacting a broker."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from src.core.intraday_momentum import sector_proxy

from src.services.monitor_equity import read_monitor_equity
from src.utils.config import get_env_value
from src.utils.market_calendar import US_MARKET_ZONE
from .intraday_monitor import MonitorContext, merge_monitor_rows


def load_monitor_sectors(rows: list[dict], mirror) -> None:
    """Read cached classifications in one bounded batch; never enrich or write."""
    for row in rows:
        row.update(sector_name=None, sector_symbol=None)
    if not rows or not mirror:
        return
    mirror = Path(mirror).expanduser().resolve()
    if not mirror.is_file():
        return
    try:
        with sqlite3.connect(mirror.as_uri() + "?mode=ro", uri=True, timeout=3) as connection:
            connection.execute("PRAGMA query_only=ON")
            profiles = {}
            symbols = list(dict.fromkeys(row["symbol"] for row in rows))
            for offset in range(0, len(symbols), 500):
                chunk = symbols[offset:offset + 500]
                profiles.update({symbol: name for symbol, name in connection.execute(
                    "SELECT symbol,sector_name FROM stock_profiles WHERE symbol IN ("
                    + ",".join("?" for _ in chunk) + ")", chunk,
                )})
        for row in rows:
            name = profiles.get(row["symbol"])
            row.update(sector_name=name, sector_symbol=sector_proxy(name))
    except (OSError, sqlite3.Error):
        pass  # Missing context must not suppress price monitoring or existing ranks.


def load_monitor_context(config, store, canonical) -> MonitorContext:
    if config.mode == "SANDBOX":
        return MonitorContext(rows=merge_monitor_rows(store.list_plans(), store.list_buy_today_drafts()))
    planning = canonical.list_plans()
    rows = merge_monitor_rows(planning["rows"], store.list_shared_buy_today_drafts())
    load_monitor_sectors(rows, config.resolved_local_mirror_path)
    context = MonitorContext(rows=rows)
    repository = config.resolved_pc_repository
    try:
        from src.risk.orb_position import OrbSettings

        inputs = canonical.orb_monitor_inputs()
        document = inputs["document"]
        if document is None:
            path = repository / "data" / "settings.json" if repository else None
            document = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}
        values = document.get("orb_settings")
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ValueError("ORB settings must be an object")
        defaults = OrbSettings().to_dict()
        context.settings = OrbSettings(**{key: float(values.get(key, value)) for key, value in defaults.items()})
    except Exception:
        context.position_error = "Shared ORB settings unavailable"
        return context
    if repository is None:
        context.position_error = context.position_error or "Desktop account equity unavailable"
        return context
    context.equity, reason = read_monitor_equity(
        repository / "data" / "monitor_equity.json",
        config.canonical_environment, inputs["account_no"], dt.datetime.now(US_MARKET_ZONE),
        max_age_seconds=float(get_env_value("WEB_MONITOR_EQUITY_MAX_AGE_SECONDS", "900")),
    )
    context.position_error = context.position_error or reason
    return context
