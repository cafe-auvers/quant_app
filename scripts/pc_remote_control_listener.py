"""Small always-on listener for remote PC control over Tailscale.

Runs on the always-on PC (launched by pc_morning_routine.ps1, alongside
main.py). Accepts three plaintext commands over TCP, one per line:

  PING              -> replies PONG v3 plus a non-secret change token and
                       affected-table scope. Used
                       by the laptop dashboard for status and to trigger one
                       TiDB reconcile only after PC state actually changes.
  CHANGE <token> <event-id> [table-1,table-2]
                    -> records a local-file token for main.py; it performs no
                       database I/O. Used by the laptop after its TiDB write.
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
import hmac
import os
import socketserver
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from src.services.coordination_change_pulse import (
    read_outbound_change_event,
    record_inbound_change_pulse,
)

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
            line = (
                self.rfile.readline(2048)
                .decode("utf-8", errors="replace")
                .strip()
            )
        except OSError:
            return

        peer = self.client_address[0]

        if line == "PING":
            event = read_outbound_change_event()
            event_id = event.event_id or "-"
            tables = ",".join(event.tables) or "-"
            self.wfile.write(
                f"PONG v3 {event_id} {tables}\n".encode("utf-8")
            )
            return

        if line.startswith("CHANGE "):
            parts = line.split(" ", 3)
            supplied = parts[1].strip() if len(parts) > 1 else ""
            event_id = parts[2].strip() if len(parts) > 2 else ""
            tables = tuple(
                item.strip()
                for item in (parts[3] if len(parts) > 3 else "").split(",")
                if item.strip() and item.strip() != "-"
            )
            if not TOKEN or not hmac.compare_digest(supplied, TOKEN):
                _log(f"CHANGE request from {peer} denied: token mismatch.")
                self.wfile.write(b"DENIED\n")
                return
            if not record_inbound_change_pulse(event_id, tables=tables):
                self.wfile.write(b"INVALID\n")
                return
            self.wfile.write(b"OK\n")
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
