"""Local JSON persistence helpers."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict


DATA_DIR = Path("data")


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    """Load JSON data, falling back to a rolling backup when needed."""
    path = Path(path)
    candidates = [path, path.with_suffix(path.suffix + ".bak")]

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data

    return default


def save_json(path: Path, data: Dict[str, Any]) -> None:
    """Persist JSON data with backup-aware atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            json.dump(data, file, indent=2)
            file.write("\n")
            tmp_path = Path(file.name)

        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))

        tmp_path.replace(path)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
