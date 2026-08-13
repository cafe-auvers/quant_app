"""Decomposed buylist UI and workflow mixins."""

from .actions import BuylistActionsMixin
from .controller import BuylistController
from .execution_controller import (BuylistExecutionController,
                                   ExecutionQueueRefreshRequest,
                                   ExecutionQueueRefreshResult)
from .monitoring import BuylistMonitoringMixin
from .orders import BuylistOrdersMixin
from .view import BuylistViewMixin


class BuylistMixin(
    BuylistViewMixin,
    BuylistActionsMixin,
    BuylistMonitoringMixin,
    BuylistOrdersMixin,
):
    """Compatibility composite retaining the existing MainWindow API."""


__all__ = [
    "BuylistExecutionController",
    "BuylistController",
    "BuylistMixin",
    "ExecutionQueueRefreshRequest",
    "ExecutionQueueRefreshResult",
]
