from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.api import build_services
from src.web.cloud_publication import CloudSnapshotPublisher
from src.web.config import load_web_config
from src.web.supabase import SupabaseCloudSnapshotBackend


def _cloud_publisher(path: Path | None):
    if path is None:
        return None
    values = json.loads(path.read_text(encoding="utf-8"))
    url = str(values.get("supabase_url") or "").strip()
    secret = str(values.get("supabase_secret_key") or "").strip()
    bucket = str(values.get("bucket") or "chart-cache").strip()
    if not url or not secret:
        raise ValueError("Publisher config requires Supabase URL and secret key")
    return CloudSnapshotPublisher(
        SupabaseCloudSnapshotBackend(url, secret, bucket=bucket)
    )


async def publish(args: argparse.Namespace) -> int:
    services = build_services(load_web_config(args.config))
    cloud = _cloud_publisher(args.publisher_config)
    scanner = await asyncio.to_thread(services.market.scanner_snapshot, limit=args.limit)
    symbols = {row["symbol"] for row in scanner.get("rows", [])}
    symbols.update(services.store.pinned_symbols())
    symbols.update(value.strip().upper() for value in args.symbol if value.strip())
    failures: list[str] = []
    published = 0
    for symbol in sorted(symbols):
        for timeframe in args.timeframe:
            try:
                await services.charts.get(symbol, timeframe, refresh=args.refresh)
                if cloud is not None:
                    artifact = await asyncio.to_thread(
                        services.cache.publication_artifact, symbol, timeframe
                    )
                    if artifact is None:
                        raise RuntimeError("validated local publication artifact is unavailable")
                    await asyncio.to_thread(
                        cloud.publish,
                        symbol,
                        timeframe,
                        artifact.content,
                        artifact.metadata,
                    )
                published += 1
                print(f"READY {symbol} {timeframe}{' CLOUD' if cloud else ' LOCAL'}")
            except Exception as exc:
                failures.append(f"{symbol} {timeframe}: {exc}")
                print(f"FAILED {symbol} {timeframe}: {exc}")
    removed = services.cache.evict(pinned_symbols=services.store.pinned_symbols())
    print(f"Published {published} current bundles; failures={len(failures)}; evicted_symbols={len(removed)}")
    if cloud is None:
        print("INFO cloud publication disabled; pass --publisher-config only after policy verification")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish current local web chart snapshots")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--publisher-config",
        type=Path,
        help="Gitignored server-only Supabase publisher JSON",
    )
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--timeframe", action="append", choices=("1D", "1H"), default=[])
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if not args.timeframe:
        args.timeframe = ["1D", "1H"]
    return asyncio.run(publish(args))


if __name__ == "__main__":
    raise SystemExit(main())
