from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .drawing_import import DrawingImportReport, import_legacy_drawings
from .store import WebStore


class DesktopDrawingSyncAdapter:
    """Small opt-in bridge; never started by the web or PyQt launchers."""

    def __init__(self, store: WebStore):
        self.store = store

    def import_desktop_files(
        self, paths: Iterable[str | Path], *, dry_run: bool = True
    ) -> DrawingImportReport:
        return import_legacy_drawings(
            self.store, paths, actor="desktop-sync", dry_run=dry_run
        )

    def export_symbol_snapshot(self, symbols: Iterable[str], destination: str | Path) -> Path:
        target = Path(destination).expanduser().resolve()
        payload = {
            symbol.upper(): self.store.list_drawings(symbol)
            for symbol in sorted({value.strip().upper() for value in symbols if value.strip()})
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target
