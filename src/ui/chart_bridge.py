"""Qt WebChannel bridge for chart JavaScript callbacks."""
from __future__ import annotations

from typing import TYPE_CHECKING
import json

from PyQt5.QtCore import QObject, pyqtSlot

if TYPE_CHECKING:
    from src.ui.main_window import MainWindow


class ChartBridge(QObject):
    """Bridge chart JavaScript target-price edits back to the application."""

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window

    @pyqtSlot(str, float)
    def setChartTarget(self, symbol: str, breakout_price: float) -> None:
        self.window.set_chart_target_price(symbol, breakout_price)

    @pyqtSlot(str)
    def clearChartTarget(self, symbol: str) -> None:
        self.window.clear_chart_target_price(symbol)

    @pyqtSlot(str, str)
    def saveChartDrawing(self, symbol: str, drawing_json: str) -> None:
        self.window.save_chart_drawing(symbol, drawing_json)

    @pyqtSlot(str, str)
    def updateChartDrawing(self, symbol: str, drawing_json: str) -> None:
        self.window.update_chart_drawing(symbol, drawing_json)

    @pyqtSlot(str, str)
    def deleteChartDrawing(self, symbol: str, drawing_id: str) -> None:
        drawing_id_value = str(drawing_id)
        timeframe = None
        try:
            payload = json.loads(drawing_id)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            requested_id = payload.get("id")
            if requested_id is not None:
                drawing_id_value = str(requested_id)
            payload_timeframe = str(payload.get("timeframe", "")).strip().upper()
            timeframe = payload_timeframe or None
        self.window.delete_chart_drawing(
            symbol, drawing_id_value, timeframe=timeframe
        )

    @pyqtSlot(str)
    def clearChartDrawings(self, symbol: str) -> None:
        self.window.clear_chart_drawings(symbol)

    @pyqtSlot(str, str, str, float, bool)
    def syncChartCrosshair(
        self,
        symbol: str,
        source_view: str,
        chart_time: str,
        price: float,
        visible: bool,
    ) -> None:
        self.window.sync_tradingview_crosshair(
            symbol, source_view, chart_time, price, visible
        )

    @pyqtSlot(str, int, int)
    def updateChartWindow(self, symbol: str, visible_bars: int, visible_end: int) -> None:
        self.window.update_chart_window(symbol, visible_bars, visible_end)

    @pyqtSlot(int)
    def stepChartSymbol(self, direction: int) -> None:
        self.window.step_chart_symbol(direction)

    @pyqtSlot(str)
    def resetChartFullView(self, symbol: str) -> None:
        self.window.reset_chart_full_view(symbol)
