from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.config import load_web_config
from src.web.drawing_import import import_legacy_drawings
from src.web.store import WebStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Import legacy chart drawings into the local web store")
    parser.add_argument("source", nargs="+", type=Path, help="One or more chart_drawings.json files")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply after reviewing the default dry run")
    args = parser.parse_args()
    config = load_web_config(args.config)
    report = import_legacy_drawings(
        WebStore(config.database_path), args.source, dry_run=not args.apply
    )
    print(json.dumps(report.as_dict(), indent=2))
    return 1 if report.conflicts or report.invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
