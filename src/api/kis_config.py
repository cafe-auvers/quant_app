"""Compatibility loader for KIS PROD credentials.

Secrets live in the repository-level .env file. This module keeps the older
scripts working without storing API credentials in source code.
"""
from __future__ import annotations

import os
from typing import Optional

from src.utils.config import install_repository_configuration


DEFAULT_KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"


def _load_dotenv_file() -> None:
    install_repository_configuration()


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
