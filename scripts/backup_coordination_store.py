"""Save a verified, coherent PostgreSQL coordination backup on the PC."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.infrastructure.database.coordination_engine import (
    create_coordination_connection_engine, get_coordination_database_config,
)
from src.services.coordination_store_migration import (
    backup_coordination_snapshot, write_coordination_snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "coordination_backups")
    args = parser.parse_args()
    config = get_coordination_database_config()
    if config["backend"] != "postgresql":
        print("Automatic coordination backup requires PostgreSQL; TiDB is not queried", file=sys.stderr)
        return 1
    engine = create_coordination_connection_engine(config, read_only=False)
    try:
        snapshot = backup_coordination_snapshot(engine)
        name = datetime.now(timezone.utc).strftime("coordination_%Y%m%dT%H%M%S_%fZ.json")
        path = args.output_dir / name
        write_coordination_snapshot(path, snapshot)
        print(json.dumps({"backup": str(path), "sha256": snapshot["sha256"]}))
        return 0
    except Exception as exc:
        print(f"Coordination backup failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
