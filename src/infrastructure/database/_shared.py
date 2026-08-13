import os
import datetime as dt
import hashlib
import logging
import re
import time
import random
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import pandas as pd
import numpy as np
from sqlalchemy import (
    create_engine,
    event,
    MetaData,
    Table,
    Column,
    String,
    Float,
    DateTime,
    Boolean,
    Integer,
    select,
    text,
    func,
    delete,
    insert,
    inspect,
    tuple_,
)
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.utils.config import DATA_DIR, get_mysql_config
from src.utils.data_loader import download_price_history, _extract_symbol_history, compute_stock_metrics
from src.utils.market_calendar import expected_latest_market_data_date

_ensured_engines: weakref.WeakSet[Engine] = weakref.WeakSet()
_MYSQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
_MYSQL_HOST_FORBIDDEN_CHARACTERS = frozenset("/\\@?#")
MYSQL_CONNECT_TIMEOUT_SECONDS = 3
MYSQL_READ_WRITE_TIMEOUT_SECONDS = 15
MYSQL_POOL_RECYCLE_SECONDS = 1800
CACHE_QUERY_SYMBOL_CHUNK_SIZE = 200
HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE = 100
SCANNER_QUERY_SYMBOL_CHUNK_SIZE = 500
SCANNER_METRIC_WRITE_CHUNK_SIZE = 250
logger = logging.getLogger(__name__)

SCANNER_METRICS_CACHE_VERSION = 1
CHART_INDICATOR_CACHE_VERSION = 1
REFERENCE_SYMBOL = "SPY"



# Explicitly export private compatibility names as well as public ones.
__all__ = [name for name in globals() if not name.startswith('__')]
