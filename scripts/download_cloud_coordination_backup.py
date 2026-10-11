"""Download a verified recovery archive without mutating the running database."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from src.utils.config import get_env_value
from src.web.supabase import _api_headers
from src.services.cloud_coordination_backup import cloud_archive_to_snapshot
from src.services.coordination_store_migration import write_coordination_snapshot
from src.infrastructure.database.coordination_engine import get_coordination_database_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--slot", type=int, choices=range(1, 32), required=True)
    parser.add_argument("--checksum", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        project = str(get_coordination_database_config()["user"]).partition(".")[2]
        if args.url.rstrip("/") != f"https://{project}.supabase.co":
            raise ValueError("URL does not match the configured Supabase project")
        key = get_env_value("SUPABASE_SERVICE_ROLE_KEY", "")
        if not key:
            raise ValueError("Server publisher key is missing")
        response = httpx.get(
            f"{args.url.rstrip('/')}/storage/v1/object/authenticated/coordination-backups/coordination/slot-{args.slot:02d}.json.gz",
            headers=_api_headers(key, bearer_token=key), timeout=30,
        )
        response.raise_for_status()
        snapshot = cloud_archive_to_snapshot(response.content, args.checksum)
        write_coordination_snapshot(args.output, snapshot)
        print(json.dumps({"verified": True, "sha256": snapshot["sha256"], "tables": len(snapshot["payload"]["tables"])}))
        return 0
    except Exception as exc:
        print(f"Cloud backup download failed ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
