"""KIS overseas stock master downloader.

The dashboard uses this module to build the US stock universe before fetching
price bars from yfinance. Symbols returned here are therefore known by KIS.
"""
from __future__ import annotations

import argparse
import io
import re
import zipfile
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd
import requests


MASTER_URL_TEMPLATE = "https://new.real.download.dws.co.kr/common/master/{market}mst.cod.zip"
REQUEST_TIMEOUT_SEC = 60

US_MARKET_CODES = ("nas", "nys", "ams")
ALL_MARKET_CODES = ("nas", "nys", "ams", "shs", "shi", "szs", "szi", "tse", "hks", "hnx", "hsx")

OVERSEAS_MASTER_COLUMNS = [
    "national_code",
    "exchange_id",
    "exchange_code",
    "exchange_name",
    "symbol",
    "realtime_symbol",
    "korean_name",
    "english_name",
    "security_type",
    "currency",
    "float_position",
    "data_type",
    "base_price",
    "bid_order_size",
    "ask_order_size",
    "market_start_time",
    "market_end_time",
    "dr_yn",
    "dr_country_code",
    "industry_code",
    "has_index_components",
    "tick_size_type",
    "classification_code",
    "tick_size_detail",
]

DEFAULT_US_CACHE_PATH = Path("data/us_kis_tickers.csv")

NON_COMMON_NAME_PATTERNS = [
    r"\bPFD\b",
    r"\bPDF\b",
    r"\bPREF\b",
    r"\bPREFERRED\b",
    r"\bPRF\b",
    r"\bPREFERENCE\b",
    r"\bPERP\b",
    r"\bNON\s+CUM\b",
    r"\bNCUM\b",
    r"\bCUM(?:ULATIVE)?\b",
    r"\bDEP\s+SHS?\b",
    r"\bDP\s+SHS?\b",
    r"\bNOTE[S]?\b",
    r"\bDEB(?:ENTURE|ENTURES)?\b",
    r"\bSUB\s+DEB\b",
    r"\bSUBORDINATED\b",
    r"\bWARRANT[S]?\b",
    r"\bRIGHT[S]?\b",
]
NON_COMMON_SYMBOL_SUFFIXES = ("-UN", "-U")


def _normalise_market_code(market_code: str) -> str:
    code = str(market_code or "").strip().lower()
    if code not in ALL_MARKET_CODES:
        raise ValueError(f"Unsupported KIS overseas market code: {market_code!r}")
    return code


def _columns_for_width(width: int) -> list[str]:
    if width <= len(OVERSEAS_MASTER_COLUMNS):
        return OVERSEAS_MASTER_COLUMNS[:width]
    extras = [f"extra_{idx}" for idx in range(1, width - len(OVERSEAS_MASTER_COLUMNS) + 1)]
    return [*OVERSEAS_MASTER_COLUMNS, *extras]


def _read_cod_zip(content: bytes, market_code: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError(f"KIS master zip for {market_code} is empty")
        target_name = next((name for name in names if name.lower().endswith(".cod")), names[0])
        return zf.read(target_name)


def fetch_overseas_master_dataframe(market_code: str, session: Optional[requests.Session] = None) -> pd.DataFrame:
    """Download one KIS overseas master file and return it as a DataFrame."""
    code = _normalise_market_code(market_code)
    active_session = session or requests.Session()
    response = active_session.get(MASTER_URL_TEMPLATE.format(market=code), timeout=REQUEST_TIMEOUT_SEC)
    response.raise_for_status()

    raw_cod = _read_cod_zip(response.content, code)
    df = pd.read_table(
        io.BytesIO(raw_cod),
        sep="\t",
        encoding="cp949",
        dtype=str,
        header=None,
        keep_default_na=False,
    )
    df.columns = _columns_for_width(len(df.columns))
    df["source_market_code"] = code
    return df


def load_overseas_master(
    market_codes: Iterable[str] = ALL_MARKET_CODES,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """Download and combine KIS overseas master files for the requested markets."""
    active_session = session or requests.Session()
    frames = [fetch_overseas_master_dataframe(code, session=active_session) for code in market_codes]
    if not frames:
        return pd.DataFrame(columns=[*OVERSEAS_MASTER_COLUMNS, "source_market_code"])
    return pd.concat(frames, ignore_index=True)


def to_yfinance_symbol(kis_symbol: str) -> str:
    """Convert KIS US symbols such as BRK/B to the Yahoo Finance form BRK-B."""
    return str(kis_symbol or "").strip().upper().replace("/", "-").replace(".", "-")


def is_common_stock_like_symbol(symbol: str, name: str = "") -> bool:
    """Return True for common/ADR/class-share rows useful for stock scanning."""
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_name = str(name or "").strip().upper()
    if not normalized_symbol:
        return False

    if any(normalized_symbol.endswith(suffix) for suffix in NON_COMMON_SYMBOL_SUFFIXES):
        return False

    for pattern in NON_COMMON_NAME_PATTERNS:
        if re.search(pattern, normalized_name):
            return False
    if re.search(r"\bUNIT[S]?\b", normalized_name) and not re.search(r"\bCOMMON\s+UNIT[S]?\b", normalized_name):
        return False

    return True


def normalize_us_kis_stock_universe(master_df: pd.DataFrame) -> pd.DataFrame:
    """Return yfinance-ready symbols from KIS-registered US stock rows."""
    if master_df.empty:
        return pd.DataFrame(columns=["Symbol", "KisSymbol", "Exchange", "Name", "KoreanName", "Currency"])

    df = master_df.copy()
    for column in ("national_code", "security_type", "exchange_code", "symbol", "english_name", "korean_name", "currency"):
        if column not in df.columns:
            df[column] = ""
        df[column] = df[column].astype(str).str.strip()

    df = df[
        df["national_code"].str.upper().eq("US")
        & df["security_type"].eq("2")
        & df["symbol"].ne("")
    ].copy()
    if df.empty:
        return pd.DataFrame(columns=["Symbol", "KisSymbol", "Exchange", "Name", "KoreanName", "Currency"])

    result = pd.DataFrame(
        {
            "Symbol": df["symbol"].map(to_yfinance_symbol),
            "KisSymbol": df["symbol"].str.upper(),
            "Exchange": df["exchange_code"].str.upper(),
            "Name": df["english_name"],
            "KoreanName": df["korean_name"],
            "Currency": df["currency"].str.upper(),
        }
    )
    result = result[result["Symbol"].str.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", na=False)].copy()
    result = result[
        result.apply(lambda row: is_common_stock_like_symbol(row["Symbol"], row["Name"]), axis=1)
    ].copy()
    result = result.drop_duplicates(subset=["Symbol"]).sort_values(["Exchange", "Symbol"]).reset_index(drop=True)
    return result


def load_us_kis_stock_universe(
    cache_path: Path | str = DEFAULT_US_CACHE_PATH,
    refresh: bool = False,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """Load all KIS-registered US stocks, caching the normalized universe to CSV."""
    cache = Path(cache_path)
    if cache.exists() and not refresh:
        return pd.read_csv(cache, dtype=str).fillna("")

    master = load_overseas_master(US_MARKET_CODES, session=session)
    universe = normalize_us_kis_stock_universe(master)
    cache.parent.mkdir(parents=True, exist_ok=True)
    universe.to_csv(cache, index=False, encoding="utf-8-sig")
    return universe


def get_us_kis_stock_tickers(
    max_symbols: Optional[int] = None,
    cache_path: Path | str = DEFAULT_US_CACHE_PATH,
    refresh: bool = False,
) -> list[str]:
    """Return yfinance-ready tickers backed by the KIS US stock master."""
    universe = load_us_kis_stock_universe(cache_path=cache_path, refresh=refresh)
    symbols = [str(symbol).strip().upper() for symbol in universe.get("Symbol", []) if str(symbol).strip()]
    symbols = list(dict.fromkeys(symbols))
    if max_symbols is None or int(max_symbols) <= 0:
        return symbols
    return symbols[: int(max_symbols)]


normalise_us_kis_stock_universe = normalize_us_kis_stock_universe


def get_overseas_master_dataframe(base_dir: str | Path, val: str) -> pd.DataFrame:
    """Backward-compatible helper for the original standalone script."""
    output_dir = Path(base_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    df = fetch_overseas_master_dataframe(val)
    df.to_csv(output_dir / f"{_normalise_market_code(val)}_code.csv", index=False, encoding="utf-8-sig")
    return df


def _write_output(df: pd.DataFrame, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".xlsx":
        df.to_excel(output, index=False)
    else:
        df.to_csv(output, index=False, encoding="utf-8-sig")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download KIS overseas stock master files.")
    parser.add_argument("--market", choices=ALL_MARKET_CODES, help="Download one overseas market code.")
    parser.add_argument("--all", action="store_true", help="Download and combine every supported overseas market.")
    parser.add_argument("--us", action="store_true", help="Download KIS-registered US stocks only.")
    parser.add_argument("--refresh", action="store_true", help="Refresh cached US stock universe.")
    parser.add_argument("--output", type=Path, help="CSV/XLSX output path.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    if args.us or (not args.market and not args.all):
        df = load_us_kis_stock_universe(refresh=args.refresh)
        output = args.output or DEFAULT_US_CACHE_PATH
    elif args.all:
        df = load_overseas_master(ALL_MARKET_CODES)
        output = args.output or Path("overseas_stock_code_all.csv")
    else:
        df = fetch_overseas_master_dataframe(args.market)
        output = args.output or Path(f"{args.market}_code.csv")

    _write_output(df, output)
    print(f"Saved {len(df)} rows to {output}")


if __name__ == "__main__":
    main()
