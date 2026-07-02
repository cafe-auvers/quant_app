from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"


def load_env_file() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not ENV_FILE.exists():
        return values

    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value

    return values


def get_env_value(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(key)
    if value is not None:
        return value

    file_values = load_env_file()
    return file_values.get(key, default)


def get_mysql_config() -> Dict[str, str]:
    return {
        "host": get_env_value("MYSQL_HOST", "127.0.0.1") or "127.0.0.1",
        "port": get_env_value("MYSQL_PORT", "3306") or "3306",
        "user": get_env_value("MYSQL_USER", "root") or "root",
        "password": get_env_value("MYSQL_PASSWORD", "") or "",
        "database": get_env_value("MYSQL_DB", "quant_app") or "quant_app",
    }
