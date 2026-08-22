"""Synthetic, read-only responsiveness checks for documented hot paths.

The benchmark uses an offscreen Qt widget and in-memory SQLite databases. It
does not load credentials, connect to MySQL/KIS, or submit broker requests.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from PyQt5.QtWidgets import QApplication, QComboBox, QLabel, QListWidget  # noqa: E402

from src.ui.mixins.sidebar_mixin import SidebarMixin  # noqa: E402


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * 0.95)))]


class _SidebarBenchmark(SidebarMixin):
    def __init__(self, row_count: int) -> None:
        self.sidebar_stock_list = QListWidget()
        self.sidebar_source_combo = QComboBox()
        self.sidebar_source_combo.addItem("Universe", {"type": "universe"})
        self.sidebar_selected_label = QLabel()
        self.universe_tickers = [f"S{index:05d}" for index in range(row_count)]
        self.scanner_results_by_setup: dict[str, list[dict]] = {}
        self._sidebar_universe_extra_symbols: set[str] = set()
        self._sidebar_universe_extra_names: dict[str, str] = {}

    def on_sidebar_selection_changed(self) -> None:
        return None

    def _update_sidebar_watchlist_actions(self) -> None:
        return None


def benchmark_sidebar(row_count: int, sample_count: int) -> None:
    app = QApplication.instance() or QApplication([])
    sidebar = _SidebarBenchmark(row_count)
    started = time.perf_counter()
    sidebar.refresh_stock_sidebar()
    cold_ms = (time.perf_counter() - started) * 1000.0
    forced_rebuild_samples = []
    for _ in range(sample_count):
        sidebar.__dict__.pop("_sidebar_render_signature", None)
        started = time.perf_counter()
        sidebar.refresh_stock_sidebar()
        forced_rebuild_samples.append((time.perf_counter() - started) * 1000.0)
    samples = []
    for _ in range(sample_count):
        started = time.perf_counter()
        sidebar.refresh_stock_sidebar()
        samples.append((time.perf_counter() - started) * 1000.0)
    app.processEvents()
    forced_rebuild_median = statistics.median(forced_rebuild_samples)
    print(
        "sidebar "
        f"rows={row_count} cold_ms={cold_ms:.3f} "
        f"forced_rebuild_median_ms={forced_rebuild_median:.3f} "
        f"forced_rebuild_p95_ms={_p95(forced_rebuild_samples):.3f} "
        f"unchanged_median_ms={statistics.median(samples):.3f} "
        f"unchanged_p95_ms={_p95(samples):.3f} "
        f"unchanged_max_ms={max(samples):.3f}"
    )


def _build_price_history(symbol_count: int, *, indexed: bool) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE price_history ("
        "symbol TEXT NOT NULL, date TEXT NOT NULL, interval TEXT NOT NULL, "
        "close REAL, PRIMARY KEY(symbol, date, interval))"
    )
    base = dt.date(2024, 1, 1)
    rows: list[tuple[str, str, str, float]] = []
    for symbol_index in range(symbol_count):
        symbol = f"S{symbol_index:05d}"
        rows.extend(
            (symbol, str(base + dt.timedelta(days=day)), "1d", float(day))
            for day in range(260)
        )
        rows.extend(
            (symbol, f"2025-01-{step + 1:02d} 10:00:00", "1h", float(step))
            for step in range(10)
        )
    connection.executemany("INSERT INTO price_history VALUES (?, ?, ?, ?)", rows)
    if indexed:
        connection.execute(
            "CREATE INDEX ix_price_history_interval_date "
            "ON price_history(interval, date)"
        )
    return connection


def benchmark_watermark(symbol_count: int, sample_count: int) -> None:
    for indexed in (False, True):
        connection = _build_price_history(symbol_count, indexed=indexed)
        samples = []
        for _ in range(sample_count):
            started = time.perf_counter()
            connection.execute(
                "SELECT MAX(date) FROM price_history WHERE interval = '1d'"
            ).fetchone()
            samples.append((time.perf_counter() - started) * 1000.0)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT MAX(date) FROM price_history "
            "WHERE interval = '1d'"
        ).fetchone()
        print(
            "watermark "
            f"rows={symbol_count * 270} indexed={indexed} "
            f"median_ms={statistics.median(samples):.3f} "
            f"p95_ms={_p95(samples):.3f} plan={plan[-1]}"
        )
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sidebar-rows", type=int, default=6000)
    parser.add_argument("--db-symbols", type=int, default=2000)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    benchmark_sidebar(max(1, args.sidebar_rows), max(1, args.samples))
    benchmark_watermark(max(1, args.db_symbols), max(1, args.samples))


if __name__ == "__main__":
    main()
