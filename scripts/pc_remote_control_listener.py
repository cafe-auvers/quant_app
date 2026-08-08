"""Small always-on listener for remote PC control over Tailscale.

Runs on the always-on PC (launched by pc_morning_routine.ps1, alongside
main.py). Accepts two plaintext commands over TCP, one per line:

  PING              -> replies PONG. Unauthenticated -- reveals nothing
                       beyond "this listener is running," used by the
                       laptop's dashboard to show the PC on/off status.
  SHUTDOWN <token>  -> if <token> matches REMOTE_CONTROL_TOKEN from .env,
                       triggers Invoke-GuardedShutdown.ps1 (the same safety
                       guard the scheduled 10:00 shutdown uses -- won't kill
                       an in-progress historical.py refresh) and replies OK;
                       otherwise replies DENIED.

Reachability is gated by Tailscale itself (only devices already in the
tailnet can reach this port at all) plus a Windows Firewall rule scoped to
the Tailscale adapter (see setup_remote_control_firewall.ps1). The token is
defense in depth on top of that, not the only gate -- it exists so that
possessing network access alone (e.g. a future device added to the tailnet)
isn't sufficient to shut this PC down.
"""
from __future__ import annotations

import datetime as dt
import os
import socketserver
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

DEFAULT_PORT = 47821
PORT = int(os.getenv("REMOTE_CONTROL_PORT", str(DEFAULT_PORT)))
TOKEN = os.getenv("REMOTE_CONTROL_TOKEN", "")

GUARD_SCRIPT = REPO_ROOT / "scripts" / "Invoke-GuardedShutdown.ps1"
LOG_DIR = REPO_ROOT / "data" / "logs"
LOG_PATH = LOG_DIR / "pc_remote_control_listener.log"


def _log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            line = self.rfile.readline(256).decode("utf-8", errors="replace").strip()
        except OSError:
            return

        peer = self.client_address[0]

        if line == "PING":
            self.wfile.write(b"PONG\n")
            return

        if line.startswith("SHUTDOWN "):
            supplied = line[len("SHUTDOWN "):].strip()
            if not TOKEN:
                _log(f"SHUTDOWN request from {peer} denied: REMOTE_CONTROL_TOKEN is not configured.")
                self.wfile.write(b"DENIED\n")
                return
            if supplied != TOKEN:
                _log(f"SHUTDOWN request from {peer} denied: token mismatch.")
                self.wfile.write(b"DENIED\n")
                return

            _log(f"SHUTDOWN request from {peer} accepted -- launching guarded shutdown.")
            self.wfile.write(b"OK\n")
            subprocess.Popen(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(GUARD_SCRIPT),
                ],
                cwd=str(REPO_ROOT),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return

        self.wfile.write(b"UNKNOWN\n")

    def log_message(self, *args, **kwargs) -> None:  # silence default stderr logging
        pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    if not TOKEN:
        _log("WARNING: REMOTE_CONTROL_TOKEN is not set in .env -- all SHUTDOWN requests will be denied until it is.")
    with _Server(("0.0.0.0", PORT), _Handler) as server:
        _log(f"Listening on 0.0.0.0:{PORT}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
