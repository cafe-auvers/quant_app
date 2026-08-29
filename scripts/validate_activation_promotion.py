#!/usr/bin/env python3
"""Validate a promotion record without changing application activation state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activation_gates.promotion import build_promotion_decision  # noqa: E402


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an explicit activation promotion decision"
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--gate-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    decision = build_promotion_decision(
        _load(args.request), gate_report=_load(args.gate_report)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(decision, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(
        f"Promotion {decision['decision']}: "
        f"{len(decision['violations'])} violation(s); output={args.output}"
    )
    return 0 if decision["decision"] == "APPROVED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
