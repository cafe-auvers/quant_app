"""Read monitoring membership and position inputs without contacting a broker."""
from __future__ import annotations

import datetime as dt
import json

from src.services.monitor_equity import read_monitor_equity
from src.utils.config import get_env_value
from src.utils.market_calendar import US_MARKET_ZONE
from .intraday_monitor import MonitorContext, merge_monitor_rows


def load_monitor_context(config, store, canonical) -> MonitorContext:
    if config.mode == "SANDBOX":
        return MonitorContext(rows=merge_monitor_rows(store.list_plans(), store.list_buy_today_drafts()))
    planning = canonical.list_plans()
    rows = merge_monitor_rows(planning["rows"], store.list_shared_buy_today_drafts())
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
