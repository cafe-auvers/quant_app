#!/usr/bin/env python3
"""Prepare, inspect, or finalize Gate-4 qualification evidence."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.config import install_repository_configuration  # noqa: E402


install_repository_configuration()

from gate4.runner import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
