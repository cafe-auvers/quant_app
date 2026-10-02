from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.cache import ChartBundleCache, ChartLoadCoordinator
from src.web.config import load_web_config
from src.web.market_data import build_market_data_source


def percentile(values: list[float], value: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * value
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


async def measured_get(coordinator, symbol, timeframe, *, refresh=False):
    started = time.perf_counter()
    artifact, _cache_hit = await coordinator.get_compressed(
        symbol, timeframe, refresh=refresh
    )
    return (time.perf_counter() - started) * 1000, artifact


async def run(args: argparse.Namespace) -> dict:
    config = load_web_config(args.config)
    source = build_market_data_source(config.local_mirror_path)
    scanner = source.scanner_snapshot(limit=args.symbols)
    symbols = [str(row["symbol"]) for row in scanner["rows"]][: args.symbols]
    if len(symbols) < args.symbols:
        raise RuntimeError(
            f"Source returned {len(symbols)} symbols; {args.symbols} required"
        )
    with tempfile.TemporaryDirectory(prefix="quant-web-benchmark-") as directory:
        cache = ChartBundleCache(directory, max_symbols=max(args.symbols, 25))
        coordinator = ChartLoadCoordinator(
            source,
            cache,
            daily_bars=config.daily_bars,
            hourly_months=config.hourly_months,
            worker_limit=args.workers,
        )
        cold_navigation: list[float] = []
        cold_bundle: list[float] = []
        compressed_bytes = 0
        cold_started = time.perf_counter()
        for symbol in symbols:
            symbol_started = time.perf_counter()
            daily_ms, daily = await measured_get(coordinator, symbol, "1D")
            cold_navigation.append((time.perf_counter() - symbol_started) * 1000)
            cold_bundle.append(daily_ms)
            compressed_bytes += len(daily.content)
        cold_wall = time.perf_counter() - cold_started

        background_bundle: list[float] = []
        background_started = time.perf_counter()
        for symbol in symbols:
            hourly_ms, hourly = await measured_get(coordinator, symbol, "1H")
            background_bundle.append(hourly_ms)
            compressed_bytes += len(hourly.content)
        background_wall = time.perf_counter() - background_started

        warm_navigation: list[float] = []
        warm_bundle: list[float] = []
        warm_started = time.perf_counter()
        for symbol in symbols:
            symbol_started = time.perf_counter()
            daily_ms, _ = await measured_get(coordinator, symbol, "1D")
            warm_navigation.append((time.perf_counter() - symbol_started) * 1000)
            warm_bundle.append(daily_ms)
        warm_wall = time.perf_counter() - warm_started
        tracemalloc.start()
        cache.publication_artifact(symbols[0], "1D")
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        files = list(Path(directory).glob("*"))
        disk_bytes = sum(path.stat().st_size for path in files if path.is_file())

    def stats(values):
        return {
            "p50_ms": round(percentile(values, 0.50), 2),
            "p95_ms": round(percentile(values, 0.95), 2),
            "mean_ms": round(statistics.fmean(values), 2),
        }

    return {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source.source_name,
        "symbols": len(symbols),
        "timeframes": ["1D", "1H"],
        "requests": coordinator.request_count,
        "cold": {
            "cache_state": "empty temporary cache",
            "navigation": stats(cold_navigation),
            "bundle_job": stats(cold_bundle),
            "wall_seconds": round(cold_wall, 2),
        },
        "background": {
            "cache_state": "empty hourly cache",
            "bundle_job": stats(background_bundle),
            "wall_seconds": round(background_wall, 2),
        },
        "warm": {
            "cache_state": "validated local gzip cache",
            "navigation": stats(warm_navigation),
            "bundle_read": stats(warm_bundle),
            "wall_seconds": round(warm_wall, 2),
        },
        "compressed_bundle_bytes": compressed_bytes,
        "cache_disk_bytes_including_manifests": disk_bytes,
        "python_peak_traced_bytes": peak,
        "hardware": {
            "platform": platform.platform(),
            "processor": platform.processor() or "unreported by Python",
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
        },
        "settings": {
            "daily_bars": config.daily_bars,
            "hourly_months": config.hourly_months,
            "workers": args.workers,
        },
    }


def markdown(report: dict) -> str:
    cold = report["cold"]
    background = report["background"]
    warm = report["warm"]
    hardware = report["hardware"]
    settings = report["settings"]
    mib = 1024 * 1024
    return f"""# Web Review Performance Report

Recorded: {report['recorded_at']}

This is a reproducible local server/data-path benchmark, not a latency
guarantee. It follows the lightweight UI path: request only the visible 1D
bundle while navigating, fill 1H in the delayed background, then repeat 1D
navigation against the validated gzip cache.

| Measurement | p50 | p95 | Mean | Wall time |
|---|---:|---:|---:|---:|
| Cold visible navigation (1D only) | {cold['navigation']['p50_ms']:.2f} ms | {cold['navigation']['p95_ms']:.2f} ms | {cold['navigation']['mean_ms']:.2f} ms | {cold['wall_seconds']:.2f} s |
| Cold visible provider + normalize + compress | {cold['bundle_job']['p50_ms']:.2f} ms | {cold['bundle_job']['p95_ms']:.2f} ms | {cold['bundle_job']['mean_ms']:.2f} ms | included above |
| Delayed background 1H preparation | {background['bundle_job']['p50_ms']:.2f} ms | {background['bundle_job']['p95_ms']:.2f} ms | {background['bundle_job']['mean_ms']:.2f} ms | {background['wall_seconds']:.2f} s |
| Warm visible navigation (direct gzip hit) | {warm['navigation']['p50_ms']:.2f} ms | {warm['navigation']['p95_ms']:.2f} ms | {warm['navigation']['mean_ms']:.2f} ms | {warm['wall_seconds']:.2f} s |
| Warm publish-ready bundle read | {warm['bundle_read']['p50_ms']:.2f} ms | {warm['bundle_read']['p95_ms']:.2f} ms | {warm['bundle_read']['mean_ms']:.2f} ms | included above |

## Conditions

- Source: `{report['source']}`.
- Cache: cold empty temporary directory, then warm validated local gzip files.
- Requests: {report['requests']} ({report['symbols']} visible cold + {report['symbols']} background + {report['symbols']} visible warm).
- Coverage settings: {settings['daily_bars']} daily bars and {settings['hourly_months']} calendar months hourly.
- Worker limit: {settings['workers']}; navigation itself is sequential to make per-symbol values comparable.
- Compressed chart bytes: {report['compressed_bundle_bytes']:,} ({report['compressed_bundle_bytes'] / mib:.2f} MiB).
- Cache disk including manifests: {report['cache_disk_bytes_including_manifests']:,} ({report['cache_disk_bytes_including_manifests'] / mib:.2f} MiB).
- Peak Python allocations for one isolated warm publication read, observed by `tracemalloc`: {report['python_peak_traced_bytes']:,} ({report['python_peak_traced_bytes'] / mib:.2f} MiB). This excludes native-library allocations and is kept outside latency timing.
- Hardware/runtime: {hardware['platform']}; processor `{hardware['processor']}`; {hardware['logical_cpus']} logical CPUs; Python {hardware['python']}.

## Limitations

- DEMO results measure deterministic local generation, normalization,
  compression, checksum, and disk cache behavior; they do not predict provider,
  LAN, TiDB, Supabase, or phone-network latency.
- Values are server/data preparation timings. Browser layout/paint and a real
  iPhone are outside this script and are checked separately with viewport
  inspection.
- The benchmark intentionally uses a temporary cache and performs no planning,
  execution, broker, KIS, or cloud writes.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark 300-symbol web review")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--symbols", type=int, default=300)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs" / "web_performance_report.md"
    )
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.symbols <= 1000:
        parser.error("--symbols must be between 1 and 1000")
    report = asyncio.run(run(args))
    args.output.write_text(markdown(report), encoding="utf-8")
    if args.json_output:
        args.json_output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
