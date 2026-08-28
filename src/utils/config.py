from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Optional

ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"
RUNTIME_CONFIG_FILE = ROOT_DIR / "config" / "runtime.json"
RUNTIME_LOCAL_CONFIG_FILE = ROOT_DIR / "config" / "runtime.local.json"
DATA_DIR = ROOT_DIR / "data"
RULEBOOK_DIR = ROOT_DIR / "rulebooks"
DEFAULT_KIS_TOKEN_CACHE = ROOT_DIR / ".kis_token_cache_prod.json"
_CONFIG_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def resolve_repo_path(path: str | Path) -> Path:
    """Resolve application-owned relative paths against the repository root."""
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else ROOT_DIR / candidate


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


def _runtime_value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)


def _load_runtime_mapping(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Runtime configuration must be a JSON object: {path}")
    values: Dict[str, str] = {}
    for raw_key, value in payload.items():
        key = str(raw_key or "").strip()
        if not _CONFIG_KEY_RE.fullmatch(key):
            raise ValueError(f"Invalid runtime configuration key {key!r} in {path}")
        values[key] = _runtime_value_text(value)
    return values


def load_runtime_config(
    defaults_path: str | Path | None = None,
    local_path: str | Path | None = None,
) -> Dict[str, str]:
    """Load tracked non-secret defaults plus the workstation-local override."""

    defaults_file = Path(defaults_path or RUNTIME_CONFIG_FILE)
    local_file = Path(local_path or RUNTIME_LOCAL_CONFIG_FILE)
    defaults = _load_runtime_mapping(defaults_file)
    local = _load_runtime_mapping(local_file)
    unknown = sorted(set(local) - set(defaults))
    if unknown:
        raise ValueError(
            "Unknown local runtime configuration key(s): " + ", ".join(unknown)
        )
    defaults.update(local)
    return defaults


def repository_configuration_values() -> Dict[str, str]:
    """Return file-backed configuration without exposing it in source control.

    Runtime settings are non-secret JSON. Credential values from ``.env`` win
    only if a legacy duplicate survived migration; normal synchronized files
    have no overlapping keys.
    """

    values = load_runtime_config()
    values.update(load_env_file())
    return values


def install_repository_configuration() -> None:
    """Fill missing process variables for modules that resolve at import time."""

    for key, value in repository_configuration_values().items():
        os.environ.setdefault(key, value)


def get_env_value(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(key)
    if value is not None:
        return value

    file_values = load_env_file()
    if key in file_values:
        return file_values[key]
    return load_runtime_config().get(key, default)


def get_mysql_config() -> Dict[str, str]:
    """Return explicitly configured MySQL settings.

    MySQL is optional for this desktop application.  Do not silently attempt
    to log in as a local ``root`` user when no settings were supplied: that
    both delays startup and assumes privileges a normal application account
    should not need.
    """
    return {
        "host": get_env_value("MYSQL_HOST", "") or "",
        "port": get_env_value("MYSQL_PORT", "3306") or "3306",
        "user": get_env_value("MYSQL_USER", "") or "",
        "password": get_env_value("MYSQL_PASSWORD", "") or "",
        "database": get_env_value("MYSQL_DB", "quant_app") or "quant_app",
    }
