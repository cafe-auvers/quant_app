"""Decomposed buylist UI and workflow mixins."""
from __future__ import annotations

import sys
import types

from src.ui.buylist import _shared, actions, monitoring, orders, view
from src.ui.buylist.actions import BuylistActionsMixin
from src.ui.buylist.controller import BuylistController
from src.ui.buylist.execution_controller import (
    BuylistExecutionController,
    ExecutionQueueRefreshRequest,
    ExecutionQueueRefreshResult,
)
from src.ui.buylist.monitoring import BuylistMonitoringMixin
from src.ui.buylist.orders import BuylistOrdersMixin
from src.ui.buylist.view import BuylistViewMixin

_P2_MODULES = (_shared, view, actions, monitoring, orders)
_P2_EXPORTS = {}
for _module in _P2_MODULES:
    _P2_EXPORTS.update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )
globals().update(_P2_EXPORTS)

for _module in _P2_MODULES:
    for _name, _value in _P2_EXPORTS.items():
        _module.__dict__.setdefault(_name, _value)


class BuylistMixin(
    BuylistViewMixin,
    BuylistActionsMixin,
    BuylistMonitoringMixin,
    BuylistOrdersMixin,
):
    """Compatibility composite retaining the original MainWindow API."""


for _module in _P2_MODULES:
    _module.__dict__["BuylistMixin"] = BuylistMixin


class _BuylistFacadeModule(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("_P2_"):
            return
        for module in _P2_MODULES:
            if name in module.__dict__:
                module.__dict__[name] = value


sys.modules[__name__].__class__ = _BuylistFacadeModule
__all__ = [
    "BuylistExecutionController",
    "BuylistController",
    "BuylistMixin",
    "ExecutionQueueRefreshRequest",
    "ExecutionQueueRefreshResult",
]
