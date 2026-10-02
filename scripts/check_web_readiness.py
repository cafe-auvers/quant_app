from __future__ import annotations

import argparse
import importlib.metadata
import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.config import WebConfigError, load_web_config
from src.web.canonical_planning import build_canonical_planning_source
from src.web.market_data import MarketDataUnavailable, ReadOnlyMirrorMarketDataSource


def _user_count(path: Path) -> int | None:
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_users'"
        ).fetchone()
        if not exists:
            return 0
        return int(connection.execute("SELECT COUNT(*) FROM web_users").fetchone()[0])
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Quant Web readiness check")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    failures = 0
    try:
        config = load_web_config(args.config)
    except WebConfigError as exc:
        print(f"FAIL config: {exc}")
        return 2

    print(f"PASS mode: {config.mode}")
    print(f"PASS bind: {config.host}:{config.port} (loopback)")
    print(f"PASS URL: {config.base_url}")
    print(f"PASS canonical planning writes: {'OFF' if not config.canonical_planning_writes else 'ON'}")
    print(
        "PASS connected operator operations: "
        + (", ".join(config.connected_operator_operations) or "OFF")
    )
    for package in ("fastapi", "uvicorn", "argon2-cffi"):
        try:
            version = importlib.metadata.version(package)
            print(f"PASS dependency {package}: {version}")
        except importlib.metadata.PackageNotFoundError:
            print(f"FAIL dependency {package}: not installed")
            failures += 1

    users = _user_count(config.database_path)
    if users:
        print(f"PASS local user bootstrap: {users} user(s)")
    else:
        print("FAIL local user bootstrap: run scripts/bootstrap_web_user.py")
        failures += 1
    mirror_path = config.resolved_local_mirror_path
    if mirror_path:
        try:
            source = ReadOnlyMirrorMarketDataSource(mirror_path)
            snapshot = source.scanner_snapshot(limit=1)
            print(
                f"PASS read-only mirror: {mirror_path} "
                f"(snapshot {snapshot.get('snapshot_date')})"
            )
        except MarketDataUnavailable as exc:
            print(f"FAIL read-only mirror: {exc}")
            failures += 1
    else:
        print("PASS data mode: deterministic DEMO (optional mirror not configured)")

    if config.mode == "CONNECTED" and config.canonical_planning_reads:
        canonical = build_canonical_planning_source(config)
        try:
            status = canonical.connectivity()
            if status.get("state") == "AVAILABLE":
                rows = canonical.list_plans()["rows"]
                print(
                    "PASS canonical planning reads: "
                    f"{len(rows)} current planning symbol(s); "
                    f"executor {status.get('executor', 'UNKNOWN')}"
                )
            else:
                print(
                    "FAIL canonical planning reads: "
                    f"{status.get('reason') or 'unavailable'}"
                )
                failures += 1
        finally:
            canonical.close()

    print("BLOCKED optional Supabase verification: credentials/project not configured" if not config.supabase_enabled else "INFO Supabase configured; run integration tests separately")
    print("PASS broker/KIS boundary: readiness check does not import or contact either")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
