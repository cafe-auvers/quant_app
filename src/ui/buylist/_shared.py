from __future__ import annotations

import datetime as dt
import html
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTabWidget,
    QDockWidget,
    QLabel,
    QPushButton,
    QLineEdit,
    QFormLayout,
    QTableWidget,
    QTableWidgetItem,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QCheckBox,
    QSpinBox,
    QTextEdit,
    QProgressBar,
    QMessageBox,
    QGroupBox,
    QHeaderView,
    QAbstractItemView,
    QSizePolicy,
    QShortcut,
    QDialog,
    QKeySequenceEdit,
    QScrollArea,
    QTextBrowser,
    QSplitter,
    QSlider,
    QDialogButtonBox,
    QMenu,
)
from PyQt5.QtCore import Qt, QThread, QTimer, QUrl
from PyQt5.QtGui import QColor, QKeySequence

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from src.risk.position_sizer import PositionSizer
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrder,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OPEN_ORDER_STATUSES,
)
from src.core.orb import (
    calculate_orb_range,
    evaluate_orb_entry_signal,
    resample_intraday_bars,
)
from src.core.scanner import StockScanner, ComparisonOperator, ScanRule
from src.core.watchlist import (
    Watchlist,
    TradePlanManager,
    TradePlan,
    BuylistManager,
    BuylistItem,
)
from src.core.trade_reviewer import TradeReviewer, TradeSetup
from src.utils.data_loader import (
    download_price_history,
    get_default_universe,
    _extract_symbol_history,
)
from src.utils.config import DATA_DIR
from src.utils.db_loader import (
    init_mysql_engine,
    load_symbol_history_from_db,
    load_hourly_history_from_db,
    get_latest_price_history_date,
    get_latest_hourly_price_history_timestamp,
    load_chart_indicators_from_db,
    calculate_chart_indicators,
    refresh_chart_indicators_for_symbol,
    save_symbol_history_to_db,
    delete_intraday_history_for_symbol,
)
from src.utils.storage import load_json, save_json
from src.api.kis_account_snapshot_dual import (
    KisEnvironment,
    discover_account_profiles,
    load_config,
)
from src.api.kis_order import (
    format_overseas_order_price,
    is_ambiguous_order_submission_error,
)
from src.services.app_state import (
    SCANNER_SETUPS_FILE,
    SETTINGS_FILE,
    archive_non_production_execution_queue_state,
    load_buylist_state,
    load_chart_drawings_state,
    load_scanner_setups_state,
    load_tab_options_state,
    load_trade_plans_state,
    load_watchlist_state,
    quarantine_rejected_records,
    save_app_state,
)
from src.services.intraday_data_service import (
    format_intraday_source_label,
    load_best_intraday_history,
)
from src.ui.chart_bridge import ChartBridge
from src.ui.dialogs import SettingsDialog, AddFilterDialog
from src.ui.filter_catalog import (
    DEFAULT_SCANNER_SETUPS,
    DEFAULT_SETTINGS,
    DEFAULT_TAB_OPTIONS,
    FILTER_CATALOG,
    SCANNER_METRICS_LABELS,
)
from src.ui.workers import (
    FxRateWorker,
    IntradayBulkFetchWorker,
    IntradayFetchWorker,
    KisAccountWorker,
    KisOrderCancelWorker,
    KisOrderQueryWorker,
    KisOrderWorker,
    KisStartupAccountsWorker,
    OrderReconciliationWorker,
    ScannerWorker,
    SingleStockAiWorker,
    WatchlistAiWorker,
)
from src.services.order_ledger import (
    append_order,
    find_open_orders,
    has_open_order,
    load_order_ledger,
    merge_orders,
    save_order_ledger,
    update_order,
)
from src.utils.intraday_helpers import (
    extract_latest_opening_bar as _extract_latest_opening_bar,
    intraday_cache_needs_backfill,
    utcnow_naive as _utcnow_naive,
)

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
# Start no more than 0.5% below the observed trigger price. If it does not fill
# and the market continues lower, the existing cancel/reprice path follows it.
STOP_LOSS_SELL_LIMIT_DISCOUNT_PCT = 0.005
STOP_LOSS_REPRICE_MIN_DROP_PCT = 0.002
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)
EXECUTION_QUEUE_FILE = DATA_DIR / "execution_queue.json"


def _main_window_global(name: str, fallback):
    module = sys.modules.get("src.ui.main_window")
    return getattr(module, name, fallback) if module is not None else fallback



# Submixins need private helpers from the former shared module namespace.
__all__ = [name for name in globals() if not name.startswith('__')]
