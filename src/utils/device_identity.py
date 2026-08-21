"""Stable PC/laptop identity helpers for cross-device controls."""
from __future__ import annotations

import ctypes
import platform
from typing import Any, Mapping, Optional

DEVICE_KIND_PC = "PC"
DEVICE_KIND_LAPTOP = "Laptop"


def normalize_device_kind(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"laptop", "notebook", "portable"}:
        return DEVICE_KIND_LAPTOP
    if text in {"pc", "desktop", "workstation"}:
        return DEVICE_KIND_PC
    return ""


def _windows_has_system_battery() -> Optional[bool]:
    """Return whether Windows reports a system battery, or None if unknown."""

    if platform.system().lower() != "windows":
        return None

    class _SystemPowerStatus(ctypes.Structure):
        _fields_ = (
            ("ac_line_status", ctypes.c_ubyte),
            ("battery_flag", ctypes.c_ubyte),
            ("battery_life_percent", ctypes.c_ubyte),
            ("system_status_flag", ctypes.c_ubyte),
            ("battery_life_time", ctypes.c_ulong),
            ("battery_full_life_time", ctypes.c_ulong),
        )

    try:
        status = _SystemPowerStatus()
        loaded = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
        if not loaded:
            return None
        # WinBase.h: BATTERY_FLAG_NO_BATTERY is 128.  A zero/unknown flag is
        # inconclusive rather than proof that this is a laptop.
        if int(status.battery_flag) == 128:
            return False
        if int(status.battery_flag) in {1, 2, 4, 8, 255}:
            return True
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return None


def detect_local_device_kind(
    hostname: str = "", *, has_system_battery: Optional[bool] = None
) -> str:
    """Identify this machine without relying on its arbitrary hostname."""

    has_battery = (
        _windows_has_system_battery()
        if has_system_battery is None
        else bool(has_system_battery)
    )
    if has_battery is True:
        return DEVICE_KIND_LAPTOP
    if has_battery is False:
        return DEVICE_KIND_PC
    return (
        DEVICE_KIND_LAPTOP
        if "laptop" in str(hostname or "").strip().lower()
        else DEVICE_KIND_PC
    )


def runtime_device_kind(
    hostname: str,
    details: Optional[Mapping[str, Any]] = None,
) -> str:
    """Resolve a published kind, with hostname fallback for old runtimes."""

    published = normalize_device_kind((details or {}).get("device_kind"))
    if published:
        return published
    return (
        DEVICE_KIND_LAPTOP
        if "laptop" in str(hostname or "").strip().lower()
        else DEVICE_KIND_PC
    )
