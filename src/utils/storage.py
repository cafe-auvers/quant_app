"""Local JSON persistence helpers."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict


DATA_DIR = Path("data")

# Windows occasionally holds a transient read/write lock on a file that's just
# been closed (antivirus scan, OneDrive sync, search indexer), which makes
# os.replace()/shutil.copy2() fail with PermissionError: [WinError 5] Access
# is denied even though nothing in this process is holding it open. Retrying
# briefly clears these up without giving up the write entirely.
_REPLACE_RETRY_ATTEMPTS = 5
_REPLACE_RETRY_DELAY_SECONDS = 0.2


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
            _retry_on_transient_oserror(lambda: shutil.copy2(path, path.with_suffix(path.suffix + ".bak")))

        _retry_on_transient_oserror(lambda: tmp_path.replace(path))
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _retry_on_transient_oserror(action) -> None:
    for attempt in range(1, _REPLACE_RETRY_ATTEMPTS + 1):
        try:
            action()
            return
        except OSError:
            if attempt == _REPLACE_RETRY_ATTEMPTS:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS * attempt)
