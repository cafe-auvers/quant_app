"""Lazy compatibility exports for UI workflow controllers.

Keeping this package initializer free of eager imports prevents focused UI
packages from cycling back through every controller while they initialize.
"""
from __future__ import annotations

from importlib import import_module
from typing import Dict, Tuple


_EXPORTS: Dict[str, Tuple[str, str]] = {
    "AccountController": (
        "src.ui.controllers.account_controller",
        "AccountController",
    ),
    "BuylistController": (
        "src.ui.buylist.controller",
        "BuylistController",
    ),
    "BuylistExecutionController": (
        "src.ui.buylist.execution_controller",
        "BuylistExecutionController",
    ),
    "ExecutionQueueRefreshRequest": (
        "src.ui.buylist.execution_controller",
        "ExecutionQueueRefreshRequest",
    ),
    "ExecutionQueueRefreshResult": (
        "src.ui.buylist.execution_controller",
        "ExecutionQueueRefreshResult",
    ),
    "ChartDataController": (
        "src.ui.charts.data_service",
        "ChartDataController",
    ),
    "ScannerController": (
        "src.ui.controllers.scanner_controller",
        "ScannerController",
    ),
    "WatchlistController": (
        "src.ui.controllers.watchlist_controller",
        "WatchlistController",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
