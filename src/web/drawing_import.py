from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .store import ConflictError, ValidationError, WebStore, normalize_symbol


@dataclass
class DrawingImportReport:
    dry_run: bool
    sources: list[str] = field(default_factory=list)
    discovered: int = 0
    imported: int = 0
    unchanged: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    invalid: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "sources": self.sources,
            "discovered": self.discovered,
            "imported": self.imported,
            "unchanged": self.unchanged,
            "conflicts": self.conflicts,
            "invalid": self.invalid,
        }


def stable_drawing_id(symbol: str, drawing: dict[str, Any]) -> str:
    existing = str(drawing.get("id") or "").strip()
    if existing:
        return existing
    payload = "|".join(
        [
            normalize_symbol(symbol),
            str(drawing.get("timeframe") or "1D").upper(),
            str(drawing.get("start_date") or ""),
            str(drawing.get("start_price") or ""),
            str(drawing.get("end_date") or ""),
            str(drawing.get("end_price") or ""),
        ]
    )
    return f"legacy-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _equivalent(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    keys = (
        "symbol",
        "start_date",
        "start_price",
        "end_date",
        "end_price",
        "timeframe",
    )
    return all(existing.get(key) == candidate.get(key) for key in keys)


def import_legacy_drawings(
    store: WebStore,
    source_paths: Iterable[str | Path],
    *,
    actor: str = "legacy-import",
    dry_run: bool = True,
) -> DrawingImportReport:
    report = DrawingImportReport(dry_run=dry_run)
    seen_in_run: dict[str, tuple[str, dict[str, Any]]] = {}
    for source_value in source_paths:
        source = Path(source_value).expanduser().resolve()
        report.sources.append(str(source))
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report.invalid.append({"source": str(source), "reason": str(exc)})
            continue
        if not isinstance(payload, dict):
            report.invalid.append(
                {"source": str(source), "reason": "Root must be a symbol-to-list object"}
            )
            continue
        for raw_symbol, drawings in payload.items():
            if not isinstance(drawings, list):
                report.invalid.append(
                    {"source": str(source), "symbol": str(raw_symbol), "reason": "Drawings must be a list"}
                )
                continue
            for raw in drawings:
                report.discovered += 1
                if not isinstance(raw, dict):
                    report.invalid.append(
                        {"source": str(source), "symbol": str(raw_symbol), "reason": "Drawing must be an object"}
                    )
                    continue
                try:
                    symbol = normalize_symbol(raw_symbol)
                    candidate = {
                        "id": stable_drawing_id(symbol, raw),
                        "symbol": symbol,
                        "start_date": str(raw["start_date"]),
                        "start_price": round(float(raw["start_price"]), 4),
                        "end_date": str(raw["end_date"]),
                        "end_price": round(float(raw["end_price"]), 4),
                        "timeframe": str(raw.get("timeframe") or "1D").upper(),
                    }
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    report.invalid.append(
                        {"source": str(source), "symbol": str(raw_symbol), "reason": str(exc)}
                    )
                    continue
                prior = seen_in_run.get(candidate["id"])
                if prior and prior[1] != candidate:
                    report.conflicts.append(
                        {
                            "id": candidate["id"],
                            "first_source": prior[0],
                            "second_source": str(source),
                            "reason": "Same stable ID has different coordinates",
                        }
                    )
                    continue
                seen_in_run[candidate["id"]] = (str(source), candidate)
                existing = next(
                    (
                        item
                        for item in store.list_drawings(symbol, include_deleted=True)
                        if item["id"] == candidate["id"]
                    ),
                    None,
                )
                if existing:
                    if _equivalent(existing, candidate):
                        report.unchanged += 1
                    else:
                        report.conflicts.append(
                            {
                                "id": candidate["id"],
                                "source": str(source),
                                "reason": "Stored drawing differs or is tombstoned",
                            }
                        )
                    continue
                if not dry_run:
                    try:
                        store.create_drawing(candidate, actor=actor)
                    except (ConflictError, ValidationError) as exc:
                        report.conflicts.append(
                            {"id": candidate["id"], "source": str(source), "reason": str(exc)}
                        )
                        continue
                report.imported += 1
    return report
