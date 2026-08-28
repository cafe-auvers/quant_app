"""Synchronize private credentials and non-secret runtime configuration.

Known operational settings are migrated from legacy ``.env`` files to
``config/runtime.local.json`` without changing their values. ``.env.pc`` is
regenerated from the credential-only ``.env`` with blank ``MYSQL_*`` values.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.env_sync import synchronize_environment_files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=REPO_ROOT / ".env.example")
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--pc-env", type=Path, default=REPO_ROOT / ".env.pc")
    parser.add_argument(
        "--runtime-defaults",
        type=Path,
        default=REPO_ROOT / "config" / "runtime.json",
    )
    parser.add_argument(
        "--runtime-local",
        type=Path,
        default=REPO_ROOT / "config" / "runtime.local.json",
    )
    parser.add_argument(
        "--symbol-keys",
        type=Path,
        default=REPO_ROOT / "data" / "kis_ws_symbol_keys.json",
    )
    args = parser.parse_args()

    try:
        result = synchronize_environment_files(
            args.template,
            args.env,
            args.pc_env,
            args.runtime_defaults,
            args.runtime_local,
            args.symbol_keys,
        )
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"Environment synchronization failed: {exc}", file=sys.stderr)
        return 1

    env_status = "updated" if result.env_changed else "current"
    pc_status = "updated" if result.pc_env_changed else "current"
    print(
        f"Environment sync complete: .env {env_status}; .env.pc {pc_status}; "
        f"{result.template_key_count} template keys; "
        f"{len(result.added_env_keys)} added to .env; "
        f"{len(result.migrated_runtime_keys)} runtime keys migrated; "
        f"{len(result.migrated_symbol_keys)} legacy symbol keys migrated; "
        "runtime.local.json "
        f"{'updated' if result.runtime_local_changed else 'current'}; "
        f"{result.mysql_values_blanked} MYSQL_* credentials blanked in .env.pc."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
