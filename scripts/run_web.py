from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.application import run
from src.web.config import load_web_config, with_runtime_overrides


def _run(args: argparse.Namespace) -> None:
    config = load_web_config(args.config)
    config = with_runtime_overrides(config, host=args.host, port=args.port)
    run(config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the private localhost dashboard")
    parser.add_argument("--config", type=Path, help="Web JSON config (defaults to config/web.local.json)")
    parser.add_argument("--host", help="Loopback host override")
    parser.add_argument("--port", type=int, help="Port override")
    parser.add_argument("--log-file", type=Path, help="Append startup and server errors to this file")
    args = parser.parse_args()
    if args.log_file is None:
        _run(args)
        return 0
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    with args.log_file.open("a", encoding="utf-8", buffering=1) as log:
        with redirect_stdout(log), redirect_stderr(log):
            print(f"\n[{datetime.now().astimezone().isoformat()}] Starting web dashboard", flush=True)
            try:
                _run(args)
            except Exception:
                traceback.print_exc()
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
