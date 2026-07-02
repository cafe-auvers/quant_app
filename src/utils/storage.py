"""Local JSON persistence helpers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


DATA_DIR = Path("data")


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    """Load JSON data, returning default for missing or malformed files."""
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return default

    return data if isinstance(data, dict) else default


def save_json(path: Path, data: Dict[str, Any]) -> None:
    """Persist JSON data using a simple atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
    tmp_path.replace(path)
