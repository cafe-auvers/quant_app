"""Compatibility loader for KIS PROD credentials.

Secrets live in the repository-level .env file. This module keeps the older
scripts working without storing API credentials in source code.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


DEFAULT_KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"


def _load_dotenv_file() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, fallback_name: Optional[str] = None, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if not value and fallback_name:
        value = os.environ.get(fallback_name, "").strip()
    return value or default


_load_dotenv_file()

KIS_BASE_URL = _env("KIS_PROD_BASE_URL", "KIS_BASE_URL", DEFAULT_KIS_BASE_URL)
KIS_APP_KEY = _env("KIS_PROD_APP_KEY", "KIS_APP_KEY")
KIS_APP_SECRET = _env("KIS_PROD_APP_SECRET", "KIS_APP_SECRET")
KIS_ACCOUNT_NO = _env("KIS_PROD_ACCOUNT_NO", "KIS_ACCOUNT_NO")
