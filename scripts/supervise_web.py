"""Keep the private web server running independently of the desktop executor."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]


def supervise(
    command: list[str],
    *,
    log_path: Path,
    restart_seconds: float = 60,
    wait: Callable[[float], None] = time.sleep,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        while True:
            print(f"[{datetime.now().astimezone().isoformat()}] Supervisor launching web server", file=log, flush=True)
            try:
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                reason = f"Web server exited with code {result.returncode}"
            except OSError as exc:
                reason = f"Web server could not start ({type(exc).__name__})"
            print(f"[{datetime.now().astimezone().isoformat()}] {reason}; restarting in {restart_seconds:g}s", file=log, flush=True)
            wait(restart_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    args = parser.parse_args()
    supervise(
        [
            sys.executable,
            "-u",
            str(ROOT / "scripts" / "run_web.py"),
            "--config",
            str(args.config.resolve()),
            "--log-file",
            str(args.log_file.resolve()),
        ],
        log_path=args.log_file,
    )


if __name__ == "__main__":
    main()
