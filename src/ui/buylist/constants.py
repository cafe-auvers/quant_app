"""Buylist UI timing and persistence constants."""

import datetime as dt
from zoneinfo import ZoneInfo

from src.utils.config import DATA_DIR

KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
STOP_LOSS_SELL_LIMIT_DISCOUNT_PCT = 0.005
STOP_LOSS_REPRICE_MIN_DROP_PCT = 0.002
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)
EXECUTION_QUEUE_FILE = DATA_DIR / "execution_queue.json"
