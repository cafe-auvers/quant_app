"""P2 market-data repository extraction from the legacy database loader."""
from .._shared import *  # noqa: F401,F403

def _normalize_timestamp(ts: pd.Timestamp) -> dt.datetime:
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return ts.tz_localize(None).to_pydatetime()


def _clean_symbols(symbols: List[str]) -> List[str]:
    cleaned = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    return list(dict.fromkeys(cleaned))


def _float_or_none(value) -> Optional[float]:
    if pd.isna(value):
        return None
    return float(value)


def _record_chunks(records: List[dict], chunk_size: int) -> List[List[dict]]:
    size = max(1, int(chunk_size or 1))
    return [records[index:index + size] for index in range(0, len(records), size)]


def _execute_bulk_upsert(
    conn,
    table: Table,
    records: List[dict],
    key_columns: Tuple[str, ...],
    dialect_name: str,
) -> int:
    if not records:
        return 0

    chunk_size = 5000 if dialect_name == "mysql" else 500
    rows_written = 0
    for chunk in _record_chunks(records, chunk_size):
        if dialect_name == "mysql":
            stmt = mysql_insert(table).values(chunk)
            update_cols = {
                col.name: stmt.inserted[col.name]
                for col in table.columns
                if col.name not in key_columns
            }
            conn.execute(stmt.on_duplicate_key_update(**update_cols))
        elif dialect_name == "sqlite":
            stmt = sqlite_insert(table).values(chunk)
            update_cols = {
                col.name: getattr(stmt.excluded, col.name)
                for col in table.columns
                if col.name not in key_columns
            }
            conn.execute(stmt.on_conflict_do_update(index_elements=list(key_columns), set_=update_cols))
        else:
            conn.execute(insert(table), chunk)
        rows_written += len(chunk)
    return rows_written


def _price_history_records_from_batch(
    batch_history: pd.DataFrame,
    symbols: List[str],
    interval: str = "1d",
) -> Tuple[List[dict], Dict[str, int]]:
    if batch_history.empty:
        return [], {}

    records = []
    counts: Dict[str, int] = {}
    updated_at = _utcnow_naive()
    normalized_interval = interval.strip().lower() or "1d"

    for symbol in _clean_symbols(symbols):
        symbol_df = _extract_symbol_history(batch_history, symbol)
        if symbol_df is None or symbol_df.empty:
            continue

        symbol_count = 0
        for timestamp, row in symbol_df.iterrows():
            try:
                record = {
                    "symbol": symbol,
                    "date": _normalize_timestamp(pd.Timestamp(timestamp)),
                    "interval": normalized_interval,
                    "open": _float_or_none(row.get("Open")),
                    "high": _float_or_none(row.get("High")),
                    "low": _float_or_none(row.get("Low")),
                    "close": _float_or_none(row.get("Close")),
                    "adj_close": _float_or_none(row.get("Adj Close", row.get("Close"))),
                    "volume": _float_or_none(row.get("Volume")),
                    "updated_at": updated_at,
                }
            except (TypeError, ValueError):
                continue

            if record["date"] is None or record["close"] is None:
                continue
            records.append(record)
            symbol_count += 1

        if symbol_count:
            counts[symbol] = symbol_count

    return records, counts


def _hourly_history_records_from_batch(
    batch_history: pd.DataFrame,
    symbols: List[str],
    source: str = "yfinance",
) -> Tuple[List[dict], Dict[str, int]]:
    if batch_history.empty:
        return [], {}

    records = []
    counts: Dict[str, int] = {}
    updated_at = _utcnow_naive()

    for symbol in _clean_symbols(symbols):
        symbol_df = _extract_symbol_history(batch_history, symbol)
        if symbol_df is None or symbol_df.empty:
            continue

        symbol_count = 0
        for timestamp, row in symbol_df.iterrows():
            try:
                record = {
                    "symbol": symbol,
                    "timestamp": _normalize_timestamp(pd.Timestamp(timestamp)),
                    "source": source,
                    "open": _float_or_none(row.get("Open")),
                    "high": _float_or_none(row.get("High")),
                    "low": _float_or_none(row.get("Low")),
                    "close": _float_or_none(row.get("Close")),
                    "adj_close": _float_or_none(row.get("Adj Close", row.get("Close"))),
                    "volume": _float_or_none(row.get("Volume")),
                    "updated_at": updated_at,
                }
            except (TypeError, ValueError):
                continue

            if record["timestamp"] is None or record["close"] is None:
                continue
            records.append(record)
            symbol_count += 1

        if symbol_count:
            counts[symbol] = symbol_count

    return records, counts


def save_symbol_history_to_db(symbol: str, history: pd.DataFrame, engine: Engine, interval: str = "1d") -> bool:
    if history.empty:
        return False

    records, _counts = _price_history_records_from_batch(history, [symbol], interval=interval)
    if not records:
        return False

    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    _ensure_price_history_table(engine)

    try:
        with engine.begin() as conn:
            _execute_bulk_upsert(
                conn,
                price_history,
                records,
                ("symbol", "date", "interval"),
                engine.dialect.name,
            )
        return True
    except SQLAlchemyError:
        return False


def save_universe_history_batch_to_db(
    batch_history: pd.DataFrame,
    symbols: List[str],
    engine: Engine,
    interval: str = "1d",
) -> int:
    """Bulk upsert a yfinance batch dataframe into price_history.

    Returns the number of OHLCV rows submitted for insert/update.
    """
    records, _counts = _price_history_records_from_batch(batch_history, symbols, interval=interval)
    if not records:
        return 0

    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    _ensure_price_history_table(engine)

    try:
        with engine.begin() as conn:
            return _execute_bulk_upsert(
                conn,
                price_history,
                records,
                ("symbol", "date", "interval"),
                engine.dialect.name,
            )
    except SQLAlchemyError:
        return 0


def save_hourly_history_to_db(
    symbol: str,
    history: pd.DataFrame,
    engine: Engine,
    source: str = "yfinance",
) -> bool:
    if history.empty:
        return False

    records, _counts = _hourly_history_records_from_batch(history, [symbol], source=source)
    if not records:
        return False

    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    _ensure_hourly_price_history_table(engine)

    try:
        with engine.begin() as conn:
            _execute_bulk_upsert(
                conn,
                hourly_history,
                records,
                ("symbol", "timestamp", "source"),
                engine.dialect.name,
            )
        return True
    except SQLAlchemyError:
        return False


def save_universe_hourly_history_batch_to_db(
    batch_history: pd.DataFrame,
    symbols: List[str],
    engine: Engine,
    source: str = "yfinance",
) -> int:
    """Bulk upsert yfinance 1-hour batch data into hourly_price_history."""
    records, _counts = _hourly_history_records_from_batch(batch_history, symbols, source=source)
    if not records:
        return 0

    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    _ensure_hourly_price_history_table(engine)

    try:
        with engine.begin() as conn:
            return _execute_bulk_upsert(
                conn,
                hourly_history,
                records,
                ("symbol", "timestamp", "source"),
                engine.dialect.name,
            )
    except SQLAlchemyError:
        return 0


def load_hourly_history_from_db(
    symbol: str,
    engine: Engine,
    start: Optional[dt.datetime] = None,
    end: Optional[dt.datetime] = None,
    source: Optional[str] = None,
) -> pd.DataFrame:
    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    stmt = select(hourly_history).where(hourly_history.c.symbol == symbol.strip().upper())
    if source:
        stmt = stmt.where(hourly_history.c.source == source)
    if start is not None:
        stmt = stmt.where(hourly_history.c.timestamp >= start)
    if end is not None:
        stmt = stmt.where(hourly_history.c.timestamp <= end)
    stmt = stmt.order_by(hourly_history.c.timestamp)

    try:
        _ensure_hourly_price_history_table(engine)
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except SQLAlchemyError:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=rows[0]._mapping.keys())
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize("UTC")
    df = df.set_index("timestamp").sort_index()
    df = df[["open", "high", "low", "close", "adj_close", "volume"]]
    df.columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    return df


def get_latest_hourly_price_history_timestamp(
    engine: Engine,
    symbol: Optional[str] = None,
    source: Optional[str] = None,
) -> Optional[dt.datetime]:
    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    stmt = select(func.max(hourly_history.c.timestamp))
    if symbol:
        stmt = stmt.where(hourly_history.c.symbol == symbol.strip().upper())
    if source:
        stmt = stmt.where(hourly_history.c.source == source)

    try:
        _ensure_hourly_price_history_table(engine)
        with engine.connect() as conn:
            latest_timestamp = conn.execute(stmt).scalar_one_or_none()
    except SQLAlchemyError:
        return None

    return latest_timestamp


def save_intraday_history_to_db(
    symbol: str,
    history: pd.DataFrame,
    engine: Engine,
    interval: str = "1m",
    source: str = "yfinance",
) -> bool:
    if history.empty:
        return False

    metadata = MetaData()
    intraday_history = _get_intraday_price_history_table(metadata)
    _ensure_intraday_price_history_table(engine)

    records = []
    for timestamp, row in history.iterrows():
        ts = pd.Timestamp(timestamp)
        records.append({
            "symbol": symbol.upper(),
            "timestamp": _normalize_timestamp(ts),
            "interval": interval,
            "source": source,
            "open": float(row.get("Open", row.get("Close", 0.0)) or 0.0),
            "high": float(row.get("High", row.get("Close", 0.0)) or 0.0),
            "low": float(row.get("Low", row.get("Close", 0.0)) or 0.0),
            "close": float(row.get("Close", 0.0) or 0.0),
            "volume": float(row.get("Volume", 0.0) or 0.0),
            "updated_at": _utcnow_naive(),
        })

    if not records:
        return False

    with engine.begin() as conn:
        if engine.dialect.name == "mysql":
            stmt = mysql_insert(intraday_history).values(records)
            update_cols = {
                col.name: stmt.inserted[col.name]
                for col in intraday_history.columns
                if col.name not in ("symbol", "timestamp", "interval", "source")
            }
            conn.execute(stmt.on_duplicate_key_update(**update_cols))
        else:
            stmt = sqlite_insert(intraday_history).values(records)
            update_cols = {
                col.name: stmt.excluded[col.name]
                for col in intraday_history.columns
                if col.name not in ("symbol", "timestamp", "interval", "source")
            }
            conn.execute(stmt.on_conflict_do_update(
                index_elements=["symbol", "timestamp", "interval", "source"],
                set_=update_cols,
            ))
    return True


def load_intraday_history_from_db(
    symbol: str,
    engine: Engine,
    interval: str = "1m",
    source: Optional[str] = None,
    since: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    metadata = MetaData()
    intraday_history = _get_intraday_price_history_table(metadata)

    stmt = select(intraday_history).where(
        intraday_history.c.symbol == symbol.upper(),
        intraday_history.c.interval == interval,
    )
    if source:
        stmt = stmt.where(intraday_history.c.source == source)
    if since is not None:
        stmt = stmt.where(intraday_history.c.timestamp >= since)
    stmt = stmt.order_by(intraday_history.c.timestamp)

    try:
        _ensure_intraday_price_history_table(engine)
        with engine.connect() as conn:
            df = pd.read_sql(stmt, conn)
    except SQLAlchemyError:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    return df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )[["Open", "High", "Low", "Close", "Volume"]]


def prune_intraday_history(engine: Engine, keep_days: int = 7) -> int:
    metadata = MetaData()
    intraday_history = _get_intraday_price_history_table(metadata)
    _ensure_intraday_price_history_table(engine)
    cutoff = _utcnow_naive() - dt.timedelta(days=keep_days)
    stmt = delete(intraday_history).where(intraday_history.c.timestamp < cutoff)
    with engine.begin() as conn:
        result = conn.execute(stmt)
    return int(result.rowcount or 0)


def delete_intraday_history_for_symbol(engine: Engine, symbol: str) -> int:
    metadata = MetaData()
    intraday_history = _get_intraday_price_history_table(metadata)
    _ensure_intraday_price_history_table(engine)
    stmt = delete(intraday_history).where(intraday_history.c.symbol == symbol.upper())
    with engine.begin() as conn:
        result = conn.execute(stmt)
    return int(result.rowcount or 0)


def load_symbol_history_from_db(
    symbol: str,
    engine: Engine,
    start: Optional[dt.datetime] = None,
    end: Optional[dt.datetime] = None,
    interval: str = "1d",
) -> pd.DataFrame:
    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    stmt = select(price_history).where(
        price_history.c.symbol == symbol.strip().upper(),
        price_history.c.interval == interval.strip().lower(),
    )
    if start is not None:
        stmt = stmt.where(price_history.c.date >= start)
    if end is not None:
        stmt = stmt.where(price_history.c.date <= end)
    stmt = stmt.order_by(price_history.c.date)

    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except SQLAlchemyError:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=rows[0]._mapping.keys())
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize("UTC")
    df = df.set_index("date").sort_index()
    df = df[["open", "high", "low", "close", "adj_close", "volume"]]
    df.columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    return df


def load_universe_history_from_db(
    tickers: List[str],
    engine: Engine,
    start: Optional[dt.datetime] = None,
    end: Optional[dt.datetime] = None,
    interval: str = "1d",
) -> dict[str, pd.DataFrame]:
    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    symbols = _clean_symbols(tickers)
    if not symbols:
        return {}

    rows = []
    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                stmt = select(price_history).where(
                    price_history.c.symbol.in_(chunk),
                    price_history.c.interval == interval.strip().lower(),
                )
                if start is not None:
                    stmt = stmt.where(price_history.c.date >= start)
                if end is not None:
                    stmt = stmt.where(price_history.c.date <= end)
                stmt = stmt.order_by(price_history.c.symbol, price_history.c.date)
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError:
        return {}

    if not rows:
        return {}

    df = pd.DataFrame(rows, columns=rows[0]._mapping.keys())
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize("UTC")
    df = df.set_index("date").sort_index()
    df = df[["symbol", "open", "high", "low", "close", "adj_close", "volume"]]

    result: dict[str, pd.DataFrame] = {}
    for symbol, group in df.groupby("symbol"):
        symbol_df = group.drop(columns=["symbol"]).copy()
        symbol_df.columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
        result[symbol] = symbol_df

    return result


def _rolling_percent_rank(series: pd.Series, lookback: int) -> pd.Series:
    def rank_last(window) -> float:
        values = pd.Series(window).dropna()
        if values.empty:
            return float("nan")
        return float(values.rank(pct=True, method="max").iloc[-1] * 100)

    return series.rolling(lookback, min_periods=1).apply(rank_last, raw=False)


def calculate_chart_indicators(
    symbol: str,
    history: pd.DataFrame,
    spy_history: pd.DataFrame,
    rs_sma_period: int = 50,
    rs_score_lookback: int = 252,
) -> pd.DataFrame:
    """Calculate RS vs SPY, RS score, TI65, and marker fields for chart rendering."""
    if history.empty or spy_history.empty:
        return pd.DataFrame()

    symbol = symbol.strip().upper()
    symbol_history = history.copy()
    spy = spy_history.copy()
    symbol_history.index = pd.to_datetime(symbol_history.index).tz_localize(None)
    spy.index = pd.to_datetime(spy.index).tz_localize(None)

    df = symbol_history[["Close", "Volume"]].rename(columns={"Close": "close", "Volume": "volume"})
    df["spy_close"] = spy["Close"].astype(float)
    df = df.dropna(subset=["close", "spy_close"]).sort_index()
    if df.empty:
        return pd.DataFrame()

    close = df["close"].astype(float)
    volume = df["volume"].fillna(0).astype(float)
    relative_strength = close / df["spy_close"].replace(0, pd.NA).astype(float)
    rs_sma_50 = relative_strength.rolling(rs_sma_period, min_periods=1).mean()
    rs_score_current = _rolling_percent_rank(relative_strength, rs_score_lookback)
    pct_change_today = close.pct_change() * 100.0
    avg_7 = close.rolling(7, min_periods=1).mean()
    avg_65 = close.rolling(65, min_periods=1).mean()
    ti65 = avg_7 / avg_65.replace(0, pd.NA)

    indicators = pd.DataFrame(
        {
            "symbol": symbol,
            "date": df.index,
            "relative_strength": relative_strength,
            "rs_sma_50": rs_sma_50,
            "rs_score_current": rs_score_current,
            "rs_score_yesterday": rs_score_current.shift(1),
            "rs_score_week": rs_score_current.shift(5),
            "rs_score_month": rs_score_current.shift(21),
            "pct_change_today": pct_change_today,
            "avg_7": avg_7,
            "avg_65": avg_65,
            "ti65": ti65,
            "is_ti65_bullish": ti65 >= 1.05,
            "is_ti65_bearish": ti65 <= 0.95,
            "is_9m_volume": volume >= 9000000,
            "is_plus_4pct_change": pct_change_today >= 4.0,
            "is_minus_4pct_change": pct_change_today <= -4.0,
            "is_rs_cross_up": (relative_strength > rs_sma_50) & (relative_strength.shift(1) <= rs_sma_50.shift(1)),
        }
    )
    indicators["updated_at"] = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return indicators


def calculate_chart_indicators_since(
    symbol: str,
    history: pd.DataFrame,
    spy_history: pd.DataFrame,
    start_date: dt.datetime,
    rs_sma_period: int = 50,
    rs_score_lookback: int = 252,
) -> pd.DataFrame:
    """Calculate only indicator rows at or after ``start_date``.

    Rolling outputs before the requested date are dependencies, not outputs.
    Computing the handful of required windows directly avoids repeating the
    expensive rolling-percent-rank calculation for every already-persisted
    row on each new market session.
    """
    if history.empty or spy_history.empty:
        return pd.DataFrame()

    symbol_history = history.copy()
    spy = spy_history.copy()
    symbol_history.index = pd.to_datetime(symbol_history.index).tz_localize(None)
    spy.index = pd.to_datetime(spy.index).tz_localize(None)
    df = symbol_history[["Close", "Volume"]].rename(
        columns={"Close": "close", "Volume": "volume"}
    )
    df["spy_close"] = spy["Close"].astype(float)
    df = df.dropna(subset=["close", "spy_close"]).sort_index()
    if df.empty:
        return pd.DataFrame()

    first_output = pd.Timestamp(start_date)
    if first_output.tzinfo is not None:
        first_output = first_output.tz_localize(None)
    target_positions = np.flatnonzero(df.index >= first_output)
    if len(target_positions) == 0:
        return pd.DataFrame()

    close = df["close"].astype(float).to_numpy()
    volume = df["volume"].fillna(0).astype(float).to_numpy()
    spy_close = df["spy_close"].astype(float).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        relative_strength = np.where(spy_close != 0, close / spy_close, np.nan)

    def window_mean(values: np.ndarray, position: int, size: int) -> float:
        window = values[max(0, position - size + 1):position + 1]
        valid = window[~np.isnan(window)]
        return float(valid.mean()) if len(valid) else float("nan")

    def rs_score(position: int) -> float:
        if position < 0 or np.isnan(relative_strength[position]):
            return float("nan")
        window = relative_strength[
            max(0, position - rs_score_lookback + 1):position + 1
        ]
        valid = window[~np.isnan(window)]
        if len(valid) == 0:
            return float("nan")
        return float(np.count_nonzero(valid <= relative_strength[position]) / len(valid) * 100.0)

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    rows = []
    for position in target_positions:
        position = int(position)
        current_rs = float(relative_strength[position])
        rs_sma_50 = window_mean(relative_strength, position, rs_sma_period)
        avg_7 = window_mean(close, position, 7)
        avg_65 = window_mean(close, position, 65)
        ti65 = avg_7 / avg_65 if avg_65 != 0 else float("nan")
        if position > 0:
            with np.errstate(divide="ignore", invalid="ignore"):
                pct_change_today = float((close[position] / close[position - 1] - 1.0) * 100.0)
            previous_rs = float(relative_strength[position - 1])
            previous_rs_sma = window_mean(relative_strength, position - 1, rs_sma_period)
        else:
            pct_change_today = float("nan")
            previous_rs = float("nan")
            previous_rs_sma = float("nan")

        rows.append(
            {
                "symbol": symbol.strip().upper(),
                "date": df.index[position],
                "relative_strength": current_rs,
                "rs_sma_50": rs_sma_50,
                "rs_score_current": rs_score(position),
                "rs_score_yesterday": rs_score(position - 1),
                "rs_score_week": rs_score(position - 5),
                "rs_score_month": rs_score(position - 21),
                "pct_change_today": pct_change_today,
                "avg_7": avg_7,
                "avg_65": avg_65,
                "ti65": ti65,
                "is_ti65_bullish": bool(ti65 >= 1.05),
                "is_ti65_bearish": bool(ti65 <= 0.95),
                "is_9m_volume": bool(volume[position] >= 9000000),
                "is_plus_4pct_change": bool(pct_change_today >= 4.0),
                "is_minus_4pct_change": bool(pct_change_today <= -4.0),
                "is_rs_cross_up": bool(
                    current_rs > rs_sma_50 and previous_rs <= previous_rs_sma
                ),
                "updated_at": now,
            }
        )

    return pd.DataFrame.from_records(rows)


def save_chart_indicators_to_db(symbol: str, indicators: pd.DataFrame, engine: Engine) -> bool:
    if indicators.empty:
        return False

    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    manifest_table = _get_chart_indicator_manifests_table(metadata)
    _ensure_chart_indicators_table(engine)
    _ensure_chart_indicator_manifests_table(engine)
    records = _chart_indicator_records(indicators, chart_indicators)
    if not records:
        return False

    try:
        with engine.begin() as conn:
            _invalidate_chart_indicator_manifests(
                conn, manifest_table, [symbol]
            )
            if engine.dialect.name not in ("mysql", "sqlite"):
                conn.execute(delete(chart_indicators).where(chart_indicators.c.symbol == symbol.strip().upper()))
            _execute_bulk_upsert(
                conn,
                chart_indicators,
                records,
                ("symbol", "date"),
                engine.dialect.name,
            )
        return True
    except SQLAlchemyError:
        return False


def _chart_indicator_records(indicators: pd.DataFrame, chart_indicators: Table) -> List[dict]:
    records = []
    value_columns = [column.name for column in chart_indicators.columns]
    for _, row in indicators.iterrows():
        record = {}
        for column in value_columns:
            value = row.get(column)
            if pd.isna(value):
                record[column] = None
            elif column == "date":
                record[column] = _normalize_timestamp(pd.Timestamp(value))
            elif column.startswith("is_"):
                record[column] = bool(value)
            else:
                record[column] = value
        records.append(record)
    return records


def save_chart_indicators_batch_to_db(
    records: List[dict],
    engine: Engine,
    replace_symbols: Optional[List[str]] = None,
) -> int:
    if not records:
        return 0

    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    manifest_table = _get_chart_indicator_manifests_table(metadata)
    _ensure_chart_indicators_table(engine)
    _ensure_chart_indicator_manifests_table(engine)
    replacement_symbols = _clean_symbols(replace_symbols or [])
    try:
        with engine.begin() as conn:
            _invalidate_chart_indicator_manifests(
                conn,
                manifest_table,
                [record["symbol"] for record in records if record.get("symbol")],
            )
            for chunk in _record_chunks(
                replacement_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                conn.execute(
                    delete(chart_indicators).where(
                        chart_indicators.c.symbol.in_(chunk)
                    )
                )
            return _execute_bulk_upsert(
                conn,
                chart_indicators,
                records,
                ("symbol", "date"),
                engine.dialect.name,
            )
    except SQLAlchemyError:
        return 0


def get_latest_chart_indicator_dates(
    engine: Engine, symbols: List[str]
) -> Dict[str, dt.datetime]:
    """Return the latest persisted chart-indicator date for each symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}
    rows = []
    try:
        table = _ensure_chart_indicators_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(
                cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                stmt = (
                    select(table.c.symbol, func.max(table.c.date).label("latest_date"))
                    .where(table.c.symbol.in_(chunk))
                    .group_by(table.c.symbol)
                )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError:
        return {}
    return {
        str(row.symbol).upper(): row.latest_date
        for row in rows
        if row.latest_date is not None
    }


def _history_watermark_values(
    watermark: object,
) -> Optional[Tuple[dt.datetime, int]]:
    if isinstance(watermark, dict):
        latest = watermark.get("latest_date")
        row_count = int(watermark.get("row_count") or 0)
    elif isinstance(watermark, (tuple, list)) and len(watermark) >= 2:
        latest = watermark[0]
        row_count = int(watermark[1] or 0)
    else:
        return None
    if latest is None or pd.isna(latest) or row_count <= 0:
        return None
    return _normalize_timestamp(pd.Timestamp(latest)), row_count


def _get_chart_indicator_manifests(
    engine: Engine, symbols: List[str]
) -> Dict[str, Dict[str, object]]:
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}
    rows = []
    try:
        table = _ensure_chart_indicator_manifests_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(
                cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                stmt = select(table).where(table.c.symbol.in_(chunk))
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to verify chart-indicator manifests") from exc
    return {
        str(row.symbol).upper(): {
            "reference_symbol": str(row.reference_symbol).upper(),
            "source_latest_date": row.source_latest_date,
            "source_row_count": int(row.source_row_count or 0),
            "reference_latest_date": row.reference_latest_date,
            "reference_row_count": int(row.reference_row_count or 0),
            "cache_version": int(row.cache_version or 0),
        }
        for row in rows
    }


def _save_chart_indicator_manifests(
    engine: Engine,
    symbols: List[str],
    history_watermarks: Dict[str, object],
    reference_symbol: str,
) -> None:
    reference_values = _history_watermark_values(
        history_watermarks.get(reference_symbol)
    )
    if reference_values is None:
        return
    reference_latest, reference_count = reference_values
    now = _utcnow_naive()
    records = []
    for symbol in _clean_symbols(symbols):
        source_values = _history_watermark_values(history_watermarks.get(symbol))
        if source_values is None:
            continue
        source_latest, source_count = source_values
        records.append(
            {
                "symbol": symbol,
                "reference_symbol": reference_symbol,
                "source_latest_date": source_latest,
                "source_row_count": source_count,
                "reference_latest_date": reference_latest,
                "reference_row_count": reference_count,
                "cache_version": CHART_INDICATOR_CACHE_VERSION,
                "completed_at": now,
            }
        )
    if not records:
        return
    table = _ensure_chart_indicator_manifests_table(engine)
    try:
        with engine.begin() as conn:
            for chunk in _record_chunks(
                records, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                _execute_bulk_upsert(
                    conn,
                    table,
                    chunk,
                    ("symbol",),
                    engine.dialect.name,
                )
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to save chart-indicator manifests") from exc


def _chart_indicator_manifest_matches(
    manifest: Optional[Dict[str, object]],
    source_values: Tuple[dt.datetime, int],
    reference_values: Tuple[dt.datetime, int],
    reference_symbol: str,
) -> bool:
    if manifest is None:
        return False
    source_latest, source_count = source_values
    reference_latest, reference_count = reference_values
    return (
        manifest["reference_symbol"] == reference_symbol
        and manifest["cache_version"] == CHART_INDICATOR_CACHE_VERSION
        and manifest["source_latest_date"] == source_latest
        and manifest["source_row_count"] == source_count
        and manifest["reference_latest_date"] == reference_latest
        and manifest["reference_row_count"] == reference_count
    )


def _invalidate_chart_indicator_manifests(
    conn, table: Table, symbols: List[str]
) -> None:
    """Invalidate completion metadata in the same transaction as cache DML.

    All application writes to ``chart_indicators`` must use the save helpers
    above so a matching manifest remains proof that no app write was
    interrupted or partially committed.
    """
    for chunk in _record_chunks(
        _clean_symbols(symbols), CACHE_QUERY_SYMBOL_CHUNK_SIZE
    ):
        conn.execute(delete(table).where(table.c.symbol.in_(chunk)))


def _clear_chart_indicator_cache(engine: Engine, symbols: List[str]) -> None:
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return
    metadata = MetaData()
    indicators = _get_chart_indicators_table(metadata)
    manifests = _get_chart_indicator_manifests_table(metadata)
    _ensure_chart_indicators_table(engine)
    _ensure_chart_indicator_manifests_table(engine)
    try:
        with engine.begin() as conn:
            _invalidate_chart_indicator_manifests(
                conn, manifests, cleaned_symbols
            )
            for chunk in _record_chunks(
                cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                conn.execute(
                    delete(indicators).where(indicators.c.symbol.in_(chunk))
                )
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to clear stale chart-indicator cache") from exc


def get_latest_chart_indicator_source_dates(
    engine: Engine,
    symbols: List[str],
    reference_symbol: str = "SPY",
) -> Dict[str, dt.datetime]:
    """Return each symbol's latest daily date that is also present for SPY."""
    reference_symbol = reference_symbol.strip().upper()
    cleaned_symbols = [symbol for symbol in _clean_symbols(symbols) if symbol != reference_symbol]
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    prices = _get_price_history_table(metadata)
    reference_prices = prices.alias("reference_prices")
    join_condition = (
        (prices.c.date == reference_prices.c.date)
        & (reference_prices.c.symbol == reference_symbol)
        & (reference_prices.c.interval == "1d")
    )
    rows = []
    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                stmt = (
                    select(
                        prices.c.symbol,
                        func.max(prices.c.date).label("latest_date"),
                    )
                    .select_from(prices.join(reference_prices, join_condition))
                    .where(
                        prices.c.symbol.in_(chunk), prices.c.interval == "1d"
                    )
                    .group_by(prices.c.symbol)
                )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to verify chart-indicator source dates") from exc
    return {str(row.symbol).upper(): row.latest_date for row in rows if row.latest_date is not None}


def _exact_chart_indicator_refresh_dates(
    engine: Engine,
    symbols: List[str],
    reference_symbol: str,
    force: bool,
) -> Dict[str, dt.datetime]:
    """Run exact source/indicator coverage checks in bounded symbol chunks."""
    if not symbols:
        return {}
    metadata = MetaData()
    prices = _get_price_history_table(metadata)
    reference_prices = prices.alias("chart_reference_prices")
    indicators = _get_chart_indicators_table(metadata)
    source = prices.join(
        reference_prices,
        (prices.c.date == reference_prices.c.date)
        & (reference_prices.c.symbol == reference_symbol)
        & (reference_prices.c.interval == "1d"),
    )
    _ensure_price_history_table(engine)
    _ensure_chart_indicators_table(engine)
    rows = []
    try:
        with engine.connect() as conn:
            for chunk in _record_chunks(symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                if force:
                    stmt = (
                        select(
                            prices.c.symbol,
                            func.min(prices.c.date).label("first_missing"),
                        )
                        .select_from(source)
                        .where(
                            prices.c.symbol.in_(chunk), prices.c.interval == "1d"
                        )
                        .group_by(prices.c.symbol)
                    )
                else:
                    source_with_indicators = source.outerjoin(
                        indicators,
                        (indicators.c.symbol == prices.c.symbol)
                        & (indicators.c.date == prices.c.date),
                    )
                    stmt = (
                        select(
                            prices.c.symbol,
                            func.min(prices.c.date).label("first_missing"),
                        )
                        .select_from(source_with_indicators)
                        .where(
                            prices.c.symbol.in_(chunk),
                            prices.c.interval == "1d",
                            indicators.c.date.is_(None),
                        )
                        .group_by(prices.c.symbol)
                    )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to verify chart-indicator cache coverage") from exc
    return {
        str(row.symbol).upper(): row.first_missing
        for row in rows
        if row.first_missing is not None
    }


def get_chart_indicator_refresh_plan(
    engine: Engine,
    tickers: List[str],
    reference_symbol: str = "SPY",
    force: bool = False,
    history_watermarks: Optional[Dict[str, object]] = None,
) -> Dict[str, dt.datetime]:
    """Map symbols to their earliest missing indicator source date.

    A persisted completion manifest makes the normal restart path a small
    metadata lookup. Symbols without a matching manifest use the exact
    price/SPY/indicator anti-join in bounded chunks. Exact cache hits backfill
    the manifest, so upgrading an existing database requires the audit only
    once and never recalculates already-complete indicators.
    """
    reference_symbol = reference_symbol.strip().upper()
    symbols = [symbol for symbol in _clean_symbols(tickers) if symbol != reference_symbol]
    if not symbols:
        return {}
    if history_watermarks is None:
        history_watermarks = get_price_history_watermarks(
            engine, [reference_symbol, *symbols], interval="1d", strict=True
        )
    if force:
        refresh_dates = _exact_chart_indicator_refresh_dates(
            engine,
            symbols,
            reference_symbol=reference_symbol,
            force=True,
        )
        return refresh_dates
    else:
        reference_values = _history_watermark_values(
            history_watermarks.get(reference_symbol)
        )
        if reference_values is None:
            return {}
        manifests = _get_chart_indicator_manifests(engine, symbols)
        semantic_stale = []
        coverage_unknown = []
        for symbol in symbols:
            source_values = _history_watermark_values(
                history_watermarks.get(symbol)
            )
            if source_values is None:
                continue
            manifest = manifests.get(symbol)
            if manifest is not None and (
                manifest["reference_symbol"] != reference_symbol
                or manifest["cache_version"] != CHART_INDICATOR_CACHE_VERSION
            ):
                semantic_stale.append(symbol)
            elif not _chart_indicator_manifest_matches(
                manifest,
                source_values,
                reference_values,
                reference_symbol,
            ):
                coverage_unknown.append(symbol)

    refresh_dates = _exact_chart_indicator_refresh_dates(
        engine,
        coverage_unknown,
        reference_symbol=reference_symbol,
        force=False,
    )
    semantic_refresh_dates = _exact_chart_indicator_refresh_dates(
        engine,
        semantic_stale,
        reference_symbol=reference_symbol,
        force=True,
    )
    refresh_dates.update(semantic_refresh_dates)
    semantic_empty = [
        symbol
        for symbol in semantic_stale
        if symbol not in semantic_refresh_dates
    ]
    _clear_chart_indicator_cache(engine, semantic_empty)
    complete_symbols = [
        symbol
        for symbol in coverage_unknown
        if symbol not in refresh_dates
    ]
    complete_symbols.extend(semantic_empty)
    _save_chart_indicator_manifests(
        engine,
        complete_symbols,
        history_watermarks,
        reference_symbol,
    )
    return refresh_dates


def refresh_chart_indicators_for_symbol(symbol: str, engine: Engine, reference_symbol: str = "SPY") -> bool:
    history = load_symbol_history_from_db(symbol, engine)
    spy_history = load_symbol_history_from_db(reference_symbol, engine)
    indicators = calculate_chart_indicators(symbol, history, spy_history)
    return save_chart_indicators_to_db(symbol, indicators, engine)


def refresh_chart_indicators_to_db(
    tickers: List[str],
    engine: Engine,
    reference_symbol: str = "SPY",
    log_callback: Optional[Callable[[str], None]] = None,
    force: bool = False,
    history_watermarks: Optional[Dict[str, object]] = None,
    refresh_plan: Optional[Dict[str, dt.datetime]] = None,
) -> List[str]:
    updated = []
    reference_symbol = reference_symbol.strip().upper()
    all_symbols = [symbol for symbol in _clean_symbols(tickers) if symbol != reference_symbol]
    if refresh_plan is None:
        refresh_plan = get_chart_indicator_refresh_plan(
            engine,
            all_symbols,
            reference_symbol=reference_symbol,
            force=force,
            history_watermarks=history_watermarks,
        )
    else:
        allowed_symbols = set(all_symbols)
        refresh_plan = {
            symbol: start_date
            for raw_symbol, start_date in refresh_plan.items()
            if (symbol := str(raw_symbol).strip().upper()) in allowed_symbols
        }
    symbols = list(refresh_plan)
    cached_count = len(all_symbols) - len(symbols)
    if not symbols:
        if log_callback:
            log_callback(f"Chart indicators already current for {len(all_symbols)} symbols -- skipping.")
        return []

    replacement_symbols = set(symbols if force else [])
    if not force:
        manifests = _get_chart_indicator_manifests(engine, symbols)
        replacement_symbols.update(
            symbol
            for symbol in symbols
            if (
                (manifest := manifests.get(symbol)) is not None
                and (
                    manifest["reference_symbol"] != reference_symbol
                    or manifest["cache_version"]
                    != CHART_INDICATOR_CACHE_VERSION
                )
            )
        )

    total = len(symbols)
    start_ts = time.time()
    progress_every = max(1, min(100, total // 20 or 1))
    if log_callback:
        log_callback(
            f"Calculating chart indicators: 0/{total} (0%) - "
            f"cached={cached_count}, ETA calculating..."
        )

    histories = load_universe_history_from_db(list(dict.fromkeys([reference_symbol, *symbols])), engine)
    if not histories:
        if log_callback:
            log_callback("  Failed to load cached daily histories for chart indicators.")
        return []

    spy_history = histories.get(reference_symbol)
    if spy_history is None or spy_history.empty:
        try:
            spy_history = load_symbol_history_from_db(reference_symbol, engine, interval="1d")
        except Exception:
            spy_history = None
    if spy_history is None or spy_history.empty:
        if log_callback:
            log_callback(f"  {reference_symbol}: reference history unavailable for chart indicators.")
        return []

    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    _ensure_chart_indicators_table(engine)
    pending_records: List[dict] = []
    pending_symbols: List[str] = []
    rows_saved = 0
    save_threshold = 25000

    def flush_pending() -> None:
        nonlocal pending_records, pending_symbols, rows_saved, updated
        if not pending_records:
            return
        pending_replacements = [
            symbol
            for symbol in pending_symbols
            if symbol in replacement_symbols
        ]
        if pending_replacements:
            saved_count = save_chart_indicators_batch_to_db(
                pending_records,
                engine,
                replace_symbols=pending_replacements,
            )
        else:
            saved_count = save_chart_indicators_batch_to_db(
                pending_records, engine
            )
        if saved_count:
            rows_saved += saved_count
            updated.extend(pending_symbols)
        elif log_callback:
            log_callback(f"  Failed to bulk save {len(pending_records)} chart indicator rows.")
        pending_records = []
        pending_symbols = []

    for index, symbol in enumerate(symbols, start=1):
        history = histories.get(symbol)
        if history is None or history.empty:
            if log_callback:
                log_callback(f"  {symbol}: unable to calculate chart indicators")
        else:
            indicators = calculate_chart_indicators_since(
                symbol,
                history,
                spy_history,
                start_date=refresh_plan[symbol],
            )
            records = _chart_indicator_records(indicators, chart_indicators)
            if records:
                pending_records.extend(records)
                pending_symbols.append(symbol)
            elif log_callback:
                log_callback(f"  {symbol}: unable to calculate chart indicators")

        if len(pending_records) >= save_threshold:
            flush_pending()

        if log_callback and (index == total or index % progress_every == 0):
            flush_pending()
            elapsed = time.time() - start_ts
            avg_seconds = elapsed / max(1, index)
            eta_text = _format_eta(int(avg_seconds * max(0, total - index)))
            percent = int((index / total) * 100) if total else 100
            log_callback(
                f"Chart indicators progress: {index}/{total} ({percent}%) - "
                f"symbols_saved={len(set(updated))}, rows_saved={rows_saved}, ETA {eta_text}"
            )

    flush_pending()
    return list(dict.fromkeys(updated))


def load_chart_indicators_from_db(symbol: str, engine: Engine) -> pd.DataFrame:
    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    stmt = (
        select(chart_indicators)
        .where(chart_indicators.c.symbol == symbol.strip().upper())
        .order_by(chart_indicators.c.date)
    )

    try:
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except SQLAlchemyError:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=rows[0]._mapping.keys())
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def get_latest_price_history_date(engine: Engine, interval: str = "1d") -> Optional[dt.datetime]:
    """Return the most recent market data date stored in price_history."""
    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    stmt = select(func.max(price_history.c.date)).where(price_history.c.interval == interval.strip().lower())

    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            latest_date = conn.execute(stmt).scalar_one_or_none()
    except SQLAlchemyError:
        return None

    return latest_date


def get_latest_price_history_dates(
    engine: Engine,
    symbols: List[str],
    interval: str = "1d",
    strict: bool = False,
) -> Dict[str, dt.datetime]:
    """Return latest cached daily/intraday price_history date per symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    rows = []
    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                stmt = (
                    select(
                        price_history.c.symbol,
                        func.max(price_history.c.date).label("latest_date"),
                    )
                    .where(
                        price_history.c.symbol.in_(chunk),
                        price_history.c.interval == interval.strip().lower(),
                    )
                    .group_by(price_history.c.symbol)
                )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        if strict:
            raise RuntimeError("Unable to verify latest price-history dates") from exc
        return {}

    return {str(row.symbol).upper(): row.latest_date for row in rows if row.latest_date is not None}


def get_price_history_watermarks(
    engine: Engine,
    symbols: List[str],
    interval: str = "1d",
    strict: bool = False,
) -> Dict[str, Tuple[Optional[dt.datetime], int]]:
    """Return causal cache watermarks (latest date and row count) by symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    rows = []
    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                stmt = (
                    select(
                        price_history.c.symbol,
                        func.max(price_history.c.date).label("latest_date"),
                        func.count().label("row_count"),
                    )
                    .where(
                        price_history.c.symbol.in_(chunk),
                        price_history.c.interval == interval.strip().lower(),
                    )
                    .group_by(price_history.c.symbol)
                )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        if strict:
            raise RuntimeError("Unable to verify price-history watermarks") from exc
        return {}
    return {
        str(row.symbol).upper(): (row.latest_date, int(row.row_count or 0))
        for row in rows
    }


def get_latest_hourly_price_history_timestamps(
    engine: Engine,
    symbols: List[str],
    source: Optional[str] = None,
    strict: bool = False,
) -> Dict[str, dt.datetime]:
    """Return latest cached 1-hour timestamp per symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    rows = []
    try:
        _ensure_hourly_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(
                cleaned_symbols, HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                stmt = select(
                    hourly_history.c.symbol,
                    func.max(hourly_history.c.timestamp).label("latest_timestamp"),
                ).where(hourly_history.c.symbol.in_(chunk))
                if source:
                    stmt = stmt.where(hourly_history.c.source == source)
                stmt = stmt.group_by(hourly_history.c.symbol)
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        if strict:
            raise RuntimeError("Unable to verify latest hourly-history timestamps") from exc
        return {}

    return {str(row.symbol).upper(): row.latest_timestamp for row in rows if row.latest_timestamp is not None}
