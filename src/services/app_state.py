"""Local JSON state loading and saving for the dashboard."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.watchlist import BuylistManager, TradePlanManager, Watchlist
from src.utils.storage import load_json, save_json


WATCHLIST_FILE = Path("data/watchlist.json")
BUYLIST_FILE = Path("data/buylist.json")
TRADE_PLANS_FILE = Path("data/trade_plans.json")
SCANNER_SETUPS_FILE = Path("data/scanner_setups.json")
CHART_DRAWINGS_FILE = Path("data/chart_drawings.json")
TAB_OPTIONS_FILE = Path("data/tab_options.json")
SETTINGS_FILE = Path("data/settings.json")


def load_watchlist_state() -> Watchlist:
    return Watchlist.from_dict(load_json(WATCHLIST_FILE, {"name": "Default", "items": []}))


def load_buylist_state() -> BuylistManager:
    return BuylistManager.from_dict(load_json(BUYLIST_FILE, {"items": []}))


def load_trade_plans_state() -> TradePlanManager:
    return TradePlanManager.from_dict(load_json(TRADE_PLANS_FILE, {"plans": []}))


def load_scanner_setups_state(defaults: Dict[str, Any]) -> Dict[str, Any]:
    return load_json(SCANNER_SETUPS_FILE, {"setups": defaults})


def load_tab_options_state(defaults: Dict[str, Any]) -> Dict[str, Any]:
    return load_json(TAB_OPTIONS_FILE, {"tabs": defaults})


def load_chart_drawings_state() -> Dict[str, Any]:
    data = load_json(CHART_DRAWINGS_FILE, {})
    if not isinstance(data, dict):
        return {}

    normalized = {}
    for symbol, drawings in data.items():
        symbol_key = str(symbol).strip().upper()
        if not symbol_key or not isinstance(drawings, list):
            continue
        clean_drawings = []
        for index, drawing in enumerate(drawings):
            if not isinstance(drawing, dict) or drawing.get("type") != "line":
                continue
            try:
                clean_drawings.append({
                    "id": str(drawing.get("id") or f"{symbol_key}-{index}"),
                    "type": "line",
                    "start_date": str(drawing["start_date"]),
                    "start_price": float(drawing["start_price"]),
                    "end_date": str(drawing["end_date"]),
                    "end_price": float(drawing["end_price"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        if clean_drawings:
            normalized[symbol_key] = clean_drawings
    return normalized


def save_app_state(
    watchlist_dict: Dict[str, Any],
    buylist_dict: Dict[str, Any],
    trade_manager_dict: Dict[str, Any],
    scanner_setups_copy: Any,
    chart_drawings_copy: Dict[str, Any],
    tab_options_copy: Dict[str, Any],
    *,
    save_lock: Optional[threading.Lock] = None,
) -> threading.Thread:
    lock = save_lock or threading.Lock()

    def save_worker() -> None:
        with lock:
            save_json(WATCHLIST_FILE, watchlist_dict)
            save_json(BUYLIST_FILE, buylist_dict)
            save_json(TRADE_PLANS_FILE, trade_manager_dict)
            save_json(SCANNER_SETUPS_FILE, {"setups": scanner_setups_copy})
            save_json(CHART_DRAWINGS_FILE, chart_drawings_copy)
            save_json(TAB_OPTIONS_FILE, {"tabs": tab_options_copy})

    thread = threading.Thread(target=save_worker, daemon=True)
    thread.start()
    return thread
