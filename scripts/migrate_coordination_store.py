"""Verified export/restore of the shared desktop and mobile coordination store.

Connection settings are read from separate runtime JSON and credential-only
.env files; the command never replaces the active application configuration.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.infrastructure.database.coordination_engine import (
    create_coordination_connection_engine,
    normalize_coordination_database_config,
)
from src.services.coordination_store_migration import (
    CoordinationStoreMigrationError, export_coordination_snapshot,
    provision_coordination_application_role, provision_private_coordination_schema,
    restore_coordination_snapshot, validate_coordination_snapshot,
    write_coordination_snapshot,
)
from src.web.canonical_planning import _read_env_mapping


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "restore", "verify", "provision"))
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--app-role")
    parser.add_argument("--app-credentials", type=Path)
    args = parser.parse_args()
    values = json.loads(args.runtime.read_text(encoding="utf-8-sig"))
    values.update(_read_env_mapping(args.credentials))
    if args.action in {"export", "restore", "verify"} and args.snapshot is None:
        parser.error("--snapshot is required")
    if args.action in {"export", "restore", "verify"} and not args.writers_stopped:
        parser.error("Stop every desktop/web writer and pass --writers-stopped")
    config = normalize_coordination_database_config(values)
    engine = create_coordination_connection_engine(config, read_only=False)
    try:
        if args.action == "provision":
            if not args.app_role or args.app_credentials is None:
                parser.error("--app-role and --app-credentials are required")
            password = secrets.token_urlsafe(36)
            # Save the recovery credential before creating a role. Never overwrite it.
            args.app_credentials.parent.mkdir(parents=True, exist_ok=True)
            with args.app_credentials.open("x", encoding="utf-8") as stream:
                suffix = str(config["user"]).partition(".")[2]
                user = args.app_role + ("." + suffix if suffix else "")
                stream.write(f"COORD_DB_USER={user}\nCOORD_DB_PASSWORD={password}\n")
            provision_private_coordination_schema(engine, str(config["schema"]))
            provision_coordination_application_role(engine, str(config["schema"]), args.app_role, password)
            print(json.dumps({"provisioned": True, "schema": config["schema"], "role": args.app_role}))
        elif args.action == "export":
            snapshot = export_coordination_snapshot(engine, writers_stopped=True)
            write_coordination_snapshot(args.snapshot, snapshot)
            print(json.dumps({"sha256": snapshot["sha256"], "rows": {k: len(v) for k, v in snapshot["payload"]["tables"].items()}}))
        else:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
            validate_coordination_snapshot(snapshot)
            if args.action == "restore":
                print(json.dumps(restore_coordination_snapshot(engine, snapshot)))
            else:
                actual = export_coordination_snapshot(engine, writers_stopped=True)
                if actual["payload"] != snapshot["payload"]:
                    raise CoordinationStoreMigrationError("Store differs from backup")
                print(json.dumps({"verified": True, "sha256": actual["sha256"]}))
        return 0
    except CoordinationStoreMigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Database exceptions can contain credentials, SQL parameters and private rows.
        print(f"Coordination migration failed ({type(exc).__name__}); active settings unchanged", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
