"""Synchronize gitignored environment files with ``.env.example``.

Existing values in ``.env`` are preserved.  Missing settings receive the
tracked template defaults, and ``.env.pc`` is regenerated with blank
``MYSQL_*`` values.
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
    args = parser.parse_args()

    try:
        result = synchronize_environment_files(args.template, args.env, args.pc_env)
    except (OSError, ValueError) as exc:
        print(f"Environment synchronization failed: {exc}", file=sys.stderr)
        return 1

    env_status = "updated" if result.env_changed else "current"
    pc_status = "updated" if result.pc_env_changed else "current"
    print(
        f"Environment sync complete: .env {env_status}; .env.pc {pc_status}; "
        f"{result.template_key_count} template keys; "
        f"{len(result.added_env_keys)} added to .env; "
        f"{result.mysql_values_blanked} MYSQL_* values blanked in .env.pc."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
