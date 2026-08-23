"""Client for the always-on PC's remote-control listener (see
scripts/pc_remote_control_listener.py), reached over Tailscale.

The listener supports status, guarded shutdown, and a database-free change
token used by the internal coordination pulse:
  - check_pc_status(): is the listener reachable right now ("PING" -> "PONG").
    This reports only the listener, not the PC's physical power, database, or
    main.py state. Callers must display those signals separately.
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

import socket
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.utils.config import get_env_value

DEFAULT_PORT = 47821
CONNECT_TIMEOUT_SECONDS = 3.0


class PcStatus(Enum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"  # host reachable but didn't speak the expected protocol


@dataclass(frozen=True)
class PcServiceStatus:
    """Independent health signals for the shared-data PC and its programs."""

    listener_status: PcStatus
    database_ready: bool
    database_hostname: str = ""
    main_app_active: Optional[bool] = None
    main_app_last_seen_seconds: Optional[float] = None
    coordination_change_event_id: str = ""
    coordination_change_pulse_supported: bool = False
    coordination_notification_event_id: str = ""
    coordination_notification_delivered: bool = False


@dataclass(frozen=True)
class PcListenerStatus:
    status: PcStatus
    coordination_change_event_id: str = ""
    coordination_change_pulse_supported: bool = False


def _pc_host() -> Optional[str]:
    host = (get_env_value("PC_REMOTE_CONTROL_HOST", "") or "").strip()
    return host or None


def _pc_port() -> int:
    return int(get_env_value("REMOTE_CONTROL_PORT", str(DEFAULT_PORT)) or DEFAULT_PORT)


def _token() -> str:
    return get_env_value("REMOTE_CONTROL_TOKEN", "") or ""


def check_pc_listener(timeout: float = CONNECT_TIMEOUT_SECONDS) -> PcListenerStatus:
    """Ping the listener and read its optional database-free change token."""

    host = _pc_host()
    if not host:
        return PcListenerStatus(PcStatus.OFF)
    try:
        with socket.create_connection((host, _pc_port()), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(b"PING\n")
            reply = conn.recv(256).decode("utf-8", errors="replace").strip()
    except OSError:
        return PcListenerStatus(PcStatus.OFF)
    if reply == "PONG":
        # Backward-compatible response from a listener that predates change
        # pulses. Callers retain their TiDB fallback polling in this case.
        return PcListenerStatus(PcStatus.ON)
    if reply.startswith("PONG v2"):
        parts = reply.split(" ", 2)
        event_id = "" if len(parts) < 3 or parts[2] == "-" else parts[2]
        return PcListenerStatus(
            PcStatus.ON,
            coordination_change_event_id=event_id,
            coordination_change_pulse_supported=True,
        )
    return PcListenerStatus(PcStatus.UNKNOWN)


def check_pc_status(timeout: float = CONNECT_TIMEOUT_SECONDS) -> PcStatus:
    """Compatibility status-only view of :func:`check_pc_listener`."""

    return check_pc_listener(timeout=timeout).status


def notify_pc_coordination_change(
    event_id: str, *, timeout: float = CONNECT_TIMEOUT_SECONDS
) -> bool:
    """Tell the PC listener that TiDB state has already changed.

    This request writes only a local JSON token on the PC. The PC's internal
    Python pulse notices it and performs the one canonical reconciliation.
    """

    host = _pc_host()
    token = _token()
    event_id = str(event_id or "").strip()
    if not host or not token or not event_id:
        return False
    try:
        with socket.create_connection((host, _pc_port()), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(
                f"CHANGE {token} {event_id}\n".encode("utf-8")
            )
            reply = conn.recv(64).decode("utf-8", errors="replace").strip()
    except OSError:
        return False
    return reply == "OK"


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
