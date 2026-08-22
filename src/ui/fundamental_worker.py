"""Background worker for stale chart profile/earnings refreshes."""

from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtCore import QThread, pyqtSignal
from sqlalchemy.engine import Engine

from src.core.chart_fundamentals import canonical_symbol
from src.services.chart_fundamentals import ChartFundamentalService


class ChartFundamentalRefreshWorker(QThread):
    completed = pyqtSignal(object, int)
    failed = pyqtSignal(str, str, int)

    def __init__(
        self,
        engine: Engine,
        symbol: str,
        generation: int,
        *,
        service_factory: Optional[Callable[[Engine], ChartFundamentalService]] = None,
    ):
        super().__init__()
        self.engine = engine
        self.symbol = canonical_symbol(symbol)
        self.generation = int(generation)
        self._service_factory = service_factory or ChartFundamentalService

    def run(self) -> None:
        try:
            # Provider objects and their network-backed yfinance Ticker are
            # deliberately constructed only after the QThread has started.
            service = self._service_factory(self.engine)
            context = service.refresh_symbol(self.symbol)
            self.completed.emit(context, self.generation)
        except Exception as exc:
            self.failed.emit(self.symbol, str(exc), self.generation)
