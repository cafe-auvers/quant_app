import os


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QComboBox, QLabel, QListWidget  # noqa: E402

from src.ui.mixins.sidebar_mixin import SidebarMixin  # noqa: E402


APP = QApplication.instance() or QApplication([])


class _CountingListWidget(QListWidget):
    def __init__(self):
        super().__init__()
        self.clear_calls = 0

    def clear(self) -> None:
        self.clear_calls += 1
        super().clear()


class _SidebarHarness(SidebarMixin):
    def __init__(self, symbols):
        self.sidebar_stock_list = _CountingListWidget()
        self.sidebar_source_combo = QComboBox()
        self.sidebar_source_combo.addItem("Universe", {"type": "universe"})
        self.sidebar_selected_label = QLabel()
        self.universe_tickers = list(symbols)
        self.scanner_results_by_setup = {}
        self._sidebar_universe_extra_symbols = set()
        self._sidebar_universe_extra_names = {}
        self.selection_updates = 0
        self.action_updates = 0

    def on_sidebar_selection_changed(self) -> None:
        self.selection_updates += 1

    def _update_sidebar_watchlist_actions(self) -> None:
        self.action_updates += 1


def test_unchanged_sidebar_projection_does_not_rebuild_widget_rows():
    sidebar = _SidebarHarness(["MSFT", "AAPL"])

    sidebar.refresh_stock_sidebar()
    first_items = [
        sidebar.sidebar_stock_list.item(row).text()
        for row in range(sidebar.sidebar_stock_list.count())
    ]
    sidebar.refresh_stock_sidebar()

    assert first_items == ["AAPL", "MSFT"]
    assert sidebar.sidebar_stock_list.clear_calls == 1
    assert sidebar.selection_updates == 1
    assert sidebar.action_updates == 1


def test_sidebar_projection_rebuilds_when_visible_universe_data_changes():
    sidebar = _SidebarHarness(["AAPL"])
    sidebar.refresh_stock_sidebar()

    sidebar._sidebar_universe_extra_symbols.add("NVDA")
    sidebar._sidebar_universe_extra_names["NVDA"] = "NVIDIA"
    sidebar.refresh_stock_sidebar()

    assert sidebar.sidebar_stock_list.clear_calls == 2
    assert [
        sidebar.sidebar_stock_list.item(row).data(Qt.UserRole)["symbol"]
        for row in range(sidebar.sidebar_stock_list.count())
    ] == ["AAPL", "NVDA"]
    assert sidebar.selection_updates == 2
