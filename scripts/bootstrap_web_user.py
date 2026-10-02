from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.auth import LocalAuthService
from src.web.config import ensure_runtime_directories, load_web_config
from src.web.store import ConflictError, ValidationError, WebStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the one local Quant Web user")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--username", default="owner")
    parser.add_argument("--replace", action="store_true", help="Replace the hash and revoke existing sessions")
    args = parser.parse_args()
    config = load_web_config(args.config)
    ensure_runtime_directories(config)
    store = WebStore(config.database_path)
    auth = LocalAuthService(store, session_hours=config.session_hours)
    password = getpass.getpass("New local web password (12+ characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 2
    try:
        auth.bootstrap_user(args.username, password, replace=args.replace)
    except (ConflictError, ValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Local user '{args.username.strip().lower()}' is ready. No secret value was printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
