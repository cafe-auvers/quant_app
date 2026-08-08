"""Client for the always-on PC's remote-control listener (see
scripts/pc_remote_control_listener.py), reached over Tailscale.

Two operations only:
  - check_pc_status(): is the listener reachable right now ("PING" -> "PONG").
    This answers "is the PC powered on and past logon," not "is the app
    ready" -- callers that also care about the database should check that
    separately (e.g. via init_mysql_engine()).
  - send_shutdown_signal(): sends the shared-secret-authenticated "SHUTDOWN"
    command. The listener replies immediately once it has *launched* the
    guarded shutdown script, not once the PC has actually powered off --
    Invoke-GuardedShutdown.ps1 may still wait for an in-progress refresh to
    finish before the PC actually goes down.

Waking the PC is intentionally NOT handled here -- see the dashboard's
"Wake PC" action, which just opens the router's admin page in a browser for
the user to log in and trigger it manually (no credentials stored in the
app, no scripting of the router's undocumented web UI).
"""
from __future__ import annotations

import os
import socket
from enum import Enum
from typing import Optional

DEFAULT_PORT = 47821
CONNECT_TIMEOUT_SECONDS = 3.0


class PcStatus(Enum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"  # host reachable but didn't speak the expected protocol


def _pc_host() -> Optional[str]:
    host = os.getenv("PC_REMOTE_CONTROL_HOST", "").strip()
    return host or None


def _pc_port() -> int:
    return int(os.getenv("REMOTE_CONTROL_PORT", str(DEFAULT_PORT)))


def _token() -> str:
    return os.getenv("REMOTE_CONTROL_TOKEN", "")


def check_pc_status(timeout: float = CONNECT_TIMEOUT_SECONDS) -> PcStatus:
    """Ping the listener. Returns OFF for both 'unreachable' and 'refused' --
    callers don't need to distinguish a powered-off PC from a not-yet-started
    listener; both mean "nothing usable is there right now."""
    host = _pc_host()
    if not host:
        return PcStatus.OFF
    try:
        with socket.create_connection((host, _pc_port()), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(b"PING\n")
            reply = conn.recv(64).decode("utf-8", errors="replace").strip()
    except OSError:
        return PcStatus.OFF
    return PcStatus.ON if reply == "PONG" else PcStatus.UNKNOWN


def send_shutdown_signal(timeout: float = CONNECT_TIMEOUT_SECONDS) -> "ShutdownResult":
    host = _pc_host()
    if not host:
        return ShutdownResult(False, "PC_REMOTE_CONTROL_HOST is not configured.")
    token = _token()
    if not token:
        return ShutdownResult(False, "REMOTE_CONTROL_TOKEN is not configured.")

    try:
        with socket.create_connection((host, _pc_port()), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(f"SHUTDOWN {token}\n".encode("utf-8"))
            reply = conn.recv(64).decode("utf-8", errors="replace").strip()
    except OSError as exc:
        return ShutdownResult(False, f"Could not reach the PC: {exc}")

    if reply == "OK":
        return ShutdownResult(True, "Shutdown accepted; the PC will power off once it's safe to do so.")
    if reply == "DENIED":
        return ShutdownResult(False, "PC rejected the request -- REMOTE_CONTROL_TOKEN likely doesn't match on both machines.")
    return ShutdownResult(False, f"Unexpected reply from PC: {reply!r}")


class ShutdownResult:
    __slots__ = ("accepted", "message")

    def __init__(self, accepted: bool, message: str) -> None:
        self.accepted = accepted
        self.message = message
