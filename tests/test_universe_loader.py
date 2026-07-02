import pandas as pd

import src.utils.data_loader as data_loader
from overseas_stock_code import normalize_us_kis_stock_universe, to_yfinance_symbol


def test_kis_us_master_filters_stocks_and_converts_yahoo_symbols():
    master = pd.DataFrame(
        [
            {
                "national_code": "US",
                "exchange_code": "NYS",
                "symbol": "BRK/B",
                "english_name": "BERKSHIRE HATHAWAY INC",
                "korean_name": "Berkshire",
                "security_type": "2",
                "currency": "USD",
            },
            {
                "national_code": "US",
                "exchange_code": "NAS",
                "symbol": "AAAP",
                "english_name": "PACER BARINGS CLO MARKET FLEX ETF",
                "korean_name": "ETF",
                "security_type": "3",
                "currency": "USD",
            },
            {
                "national_code": "JP",
                "exchange_code": "TSE",
                "symbol": "7203",
                "english_name": "TOYOTA MOTOR CORP",
                "korean_name": "Toyota",
                "security_type": "2",
                "currency": "JPY",
            },
        ]
    )

    result = normalize_us_kis_stock_universe(master)

    assert result["Symbol"].tolist() == ["BRK-B"]
    assert result.iloc[0]["KisSymbol"] == "BRK/B"
    assert result.iloc[0]["Exchange"] == "NYS"
    assert to_yfinance_symbol("brk/b") == "BRK-B"


def test_kis_us_master_excludes_preferred_debt_warrants_and_units():
    master = pd.DataFrame(
        [
            {
                "national_code": "US",
                "exchange_code": "NYS",
                "symbol": "C/R",
                "english_name": "CITIGROUP INC 6.250 NON CUM SER II PFD WI",
                "korean_name": "Citigroup preferred",
                "security_type": "2",
                "currency": "USD",
            },
            {
                "national_code": "US",
                "exchange_code": "NYS",
                "symbol": "F/B",
                "english_name": "FORD MOTOR CO 6.20% NOTES DUE 01/06/2059 USD25",
                "korean_name": "Ford notes",
                "security_type": "2",
                "currency": "USD",
            },
            {
                "national_code": "US",
                "exchange_code": "NYS",
                "symbol": "PSA/K",
                "english_name": "PUBLIC STORAGE 4.75% DP SHS ECH RP 1/1000TH CM PDF SR K",
                "korean_name": "Preferred typo",
                "security_type": "2",
                "currency": "USD",
            },
            {
                "national_code": "US",
                "exchange_code": "NAS",
                "symbol": "ACONW",
                "english_name": "ACLARION INC WARRANTS",
                "korean_name": "Warrant",
                "security_type": "2",
                "currency": "USD",
            },
            {
                "national_code": "US",
                "exchange_code": "NAS",
                "symbol": "AACIU",
                "english_name": "ARMADA ACQUISITION CORP I UNITS",
                "korean_name": "Unit",
                "security_type": "2",
                "currency": "USD",
            },
            {
                "national_code": "US",
                "exchange_code": "NAS",
                "symbol": "BABA",
                "english_name": "ALIBABA GROUP HOLDING LTD AMERICAN DEPOSITARY SHARES",
                "korean_name": "ADR",
                "security_type": "2",
                "currency": "USD",
            },
            {
                "national_code": "US",
                "exchange_code": "NYS",
                "symbol": "ET",
                "english_name": "ENERGY TRANSFER LP COMMON UNITS",
                "korean_name": "Common units",
                "security_type": "2",
                "currency": "USD",
            },
        ]
    )

    result = normalize_us_kis_stock_universe(master)

    assert result["Symbol"].tolist() == ["BABA", "ET"]


def test_cached_kis_universe_is_filtered(tmp_path, monkeypatch):
    cache_path = tmp_path / "us_kis_tickers.csv"
    pd.DataFrame(
        [
            {"Symbol": "AAPL", "KisSymbol": "AAPL", "Name": "APPLE INC"},
            {"Symbol": "C-R", "KisSymbol": "C/R", "Name": "CITIGROUP INC 6.250 NON CUM SER II PFD WI"},
            {"Symbol": "ACONW", "KisSymbol": "ACONW", "Name": "ACLARION INC WARRANTS"},
        ]
    ).to_csv(cache_path, index=False)
    monkeypatch.setattr(data_loader, "DEFAULT_UNIVERSE_CACHE", cache_path)

    assert data_loader.get_us_kis_tickers(refresh=False) == ["AAPL"]


def test_default_universe_uses_kis_symbols_before_sp500(monkeypatch):
    monkeypatch.setattr(
        data_loader,
        "get_us_kis_tickers",
        lambda max_symbols=None, refresh=False: ["AAA", "BBB", "CCC"][:max_symbols],
    )
    monkeypatch.setattr(data_loader, "get_sp500_tickers", lambda max_symbols=None: ["SPY"])

    assert data_loader.get_default_universe(max_symbols=2, refresh=True) == ["AAA", "BBB"]


def test_default_universe_falls_back_to_sp500_when_kis_empty(monkeypatch):
    monkeypatch.setattr(data_loader, "get_us_kis_tickers", lambda max_symbols=None, refresh=False: [])
    monkeypatch.setattr(data_loader, "get_sp500_tickers", lambda max_symbols=None: ["AAPL", "MSFT"])

    assert data_loader.get_default_universe(max_symbols=2) == ["AAPL", "MSFT"]
