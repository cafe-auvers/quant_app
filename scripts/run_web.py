from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.application import run
from src.web.config import load_web_config, with_runtime_overrides


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the private localhost dashboard")
    parser.add_argument("--config", type=Path, help="Web JSON config (defaults to config/web.local.json)")
    parser.add_argument("--host", help="Loopback host override")
    parser.add_argument("--port", type=int, help="Port override")
    args = parser.parse_args()
    config = load_web_config(args.config)
    config = with_runtime_overrides(config, host=args.host, port=args.port)
    run(config)


if __name__ == "__main__":
    main()
