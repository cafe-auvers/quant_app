"""Local JSON persistence helpers."""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


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
    failures: list[str] = []

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                raise ValueError("top-level JSON value is not an object")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            failures.append(f"{candidate.name}: {exc}")
            _preserve_corrupt_json(candidate)
            logger.warning("Ignoring unreadable JSON state %s: %s", candidate, exc)
            continue
        if candidate != path:
            logger.warning("Recovered JSON state %s from backup %s", path, candidate)
        return data

    if failures:
        logger.error(
            "No readable JSON state remained for %s; using defaults. %s",
            path,
            "; ".join(failures),
        )
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
            with contextlib.suppress(OSError):
                tmp_path.unlink()
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


def _preserve_corrupt_json(path: Path) -> None:
    """Keep a content-addressed copy before a later save can replace bad state."""
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        quarantine_path = path.with_name(f"{path.name}.corrupt-{digest}")
        if not quarantine_path.exists():
            shutil.copy2(path, quarantine_path)
    except OSError as exc:
        logger.warning("Could not preserve corrupt JSON state %s: %s", path, exc)
