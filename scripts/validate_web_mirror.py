from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.config import load_web_config
from src.web.market_data import MarketDataUnavailable, ReadOnlyMirrorMarketDataSource


def _file_state(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _first_search_symbol(
    source: ReadOnlyMirrorMarketDataSource, candidates: tuple[str, ...]
) -> str | None:
    for candidate in candidates:
        matches = source.search(candidate, limit=3)
        if any(str(row.get("symbol", "")).upper() == candidate for row in matches):
            return candidate
    return None


def _database_samples(
    path: Path, scanner_symbols: set[str]
) -> dict[str, str | None]:
    samples: dict[str, str | None] = {
        "mid_cap": None,
        "small_cap": None,
        "recent_or_short_history": None,
        "non_scanner": None,
        "earnings": None,
    }
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "price_history" in tables:
            rows = connection.execute(
                """
                SELECT symbol, COUNT(*) AS bars
                FROM price_history
                GROUP BY symbol
                HAVING COUNT(*) >= 20
                ORDER BY bars ASC, symbol ASC
                LIMIT 100
                """
            ).fetchall()
            if rows:
                samples["recent_or_short_history"] = str(rows[0][0])
            for symbol, _bars in rows:
                if str(symbol) not in scanner_symbols:
                    samples["non_scanner"] = str(symbol)
                    break
        if "earnings_events" in tables:
            row = connection.execute(
                """
                SELECT symbol FROM earnings_events
                GROUP BY symbol ORDER BY MAX(report_date) DESC LIMIT 1
                """
            ).fetchone()
            samples["earnings"] = str(row[0]) if row else None
        if "stock_profiles" in tables:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(stock_profiles)")
            }
            market_cap = next(
                (
                    name
                    for name in ("market_cap", "market_capitalization")
                    if name in columns
                ),
                None,
            )
            if market_cap:
                for label, low, high in (
                    ("mid_cap", 2_000_000_000, 10_000_000_000),
                    ("small_cap", 300_000_000, 2_000_000_000),
                ):
                    row = connection.execute(
                        f"""
                        SELECT symbol FROM stock_profiles
                        WHERE "{market_cap}" >= ? AND "{market_cap}" < ?
                        ORDER BY "{market_cap}" DESC LIMIT 1
                        """,
                        (low, high),
                    ).fetchone()
                    samples[label] = str(row[0]) if row else None
    finally:
        connection.close()
    return samples


def validate(config_path: Path) -> dict[str, Any]:
    config = load_web_config(config_path)
    mirror_path = Path(config.resolved_local_mirror_path)
    if not mirror_path.is_file():
        raise RuntimeError("Configured read-only mirror is unavailable")
    before = _file_state(mirror_path)
    source = ReadOnlyMirrorMarketDataSource(
        mirror_path,
        scanner_setups_path=(
            config.resolved_pc_repository / "data" / "scanner_setups.json"
            if config.resolved_pc_repository
            else None
        ),
    )
    health = source.health()
    scanner = source.scanner_snapshot(limit=300)
    rows = list(scanner.get("rows") or [])
    scanner_symbols = {str(row.get("symbol") or "") for row in rows}
    scores = [
        float(row["score"])
        for row in rows
        if row.get("score") is not None
    ]
    categories: dict[str, str | None] = {
        "large_cap": _first_search_symbol(source, ("AAPL", "MSFT", "NVDA")),
        "etf": _first_search_symbol(source, ("SPY", "QQQ", "IWM")),
        "scanner": str(rows[0]["symbol"]) if rows else None,
        **_database_samples(mirror_path, scanner_symbols),
    }
    symbol_results: dict[str, Any] = {}
    for category, symbol in categories.items():
        if not symbol:
            symbol_results[category] = {"status": "UNAVAILABLE"}
            continue
        result: dict[str, Any] = {"status": "READY", "symbol": symbol}
        for timeframe in ("1D", "1H"):
            try:
                bundle = source.chart_bundle(
                    symbol,
                    timeframe,
                    daily_bars=config.daily_bars,
                    hourly_months=config.hourly_months,
                )
            except MarketDataUnavailable as exc:
                result[timeframe] = {
                    "status": "UNAVAILABLE",
                    "reason": str(exc),
                }
                continue
            coverage = dict(bundle.get("coverage") or {})
            profile = dict(bundle.get("profile") or {})
            result[timeframe] = {
                "status": bundle.get("status"),
                "bars": len(bundle.get("bars") or []),
                "actual_start": coverage.get("actual_start"),
                "actual_end": coverage.get("actual_end"),
                "completeness": coverage.get("completeness"),
                "company": profile.get("company"),
                "sector": profile.get("sector"),
                "industry": profile.get("industry"),
                "leadership_context": bool(bundle.get("market_alignment")),
                "earnings": len(bundle.get("earnings") or []),
            }
        symbol_results[category] = result
    after = _file_state(mirror_path)
    return {
        "source": source.source_name,
        "health": health,
        "scanner": {
            "rows": len(rows),
            "total_matches": scanner.get("total_matches"),
            "setup": scanner.get("setup"),
            "snapshot_date": scanner.get("snapshot_date"),
            "freshness": scanner.get("freshness"),
            "scores_nonincreasing": all(
                left >= right for left, right in zip(scores, scores[1:])
            ),
            "symbols_alphabetical": [row.get("symbol") for row in rows]
            == sorted(str(row.get("symbol") or "") for row in rows),
        },
        "search": {
            "exact_aapl": any(
                str(row.get("symbol") or "").upper() == "AAPL"
                for row in source.search("AAPL", limit=10)
            ),
            "company_query_count": len(source.search("MICROSOFT", limit=10)),
        },
        "samples": symbol_results,
        "mirror_unchanged": before == after,
        "mirror_state_before": before,
        "mirror_state_after": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only web mirror validation")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate(args.config)
    payload = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["mirror_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
