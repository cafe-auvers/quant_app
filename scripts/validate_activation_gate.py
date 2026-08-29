#!/usr/bin/env python3
"""Validate Gate 3, 4, or 5 evidence without changing activation state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _builder(gate: int) -> tuple[Callable[..., dict[str, Any]], str]:
    if gate == 3:
        from gate3.reporting import build_report

        return build_report, "upstream_gate2_report"
    if gate == 4:
        from gate4.reporting import build_report

        return build_report, "upstream_gate3_report"
    if gate == 5:
        from gate5.reporting import build_report

        return build_report, "upstream_gate4_report"
    raise ValueError("Only Gates 3, 4, and 5 use this validator")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a fail-closed activation-gate report from evidence"
    )
    parser.add_argument("--gate", type=int, choices=(3, 4, 5), required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--upstream-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    build_report, upstream_name = _builder(args.gate)
    report = build_report(
        _load(args.evidence),
        **{upstream_name: _load(args.upstream_report)},
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(
        f"Gate {args.gate} {report['result']}: "
        f"{len(report['invariant_violations'])} violation(s); report={args.output}"
    )
    return 0 if report["result"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
