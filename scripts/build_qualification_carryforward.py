#!/usr/bin/env python3
"""Build and validate reviewed Gate-2/Gate-3 qualification carry-forward."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activation_gates.carryforward import (  # noqa: E402
    build_carried_gate3_report,
    build_change_manifest,
    manifest_review_subject_sha256,
    validate_carried_gate3_report,
)


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{path} must contain a JSON object")
    return value


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _reports(args: argparse.Namespace) -> tuple[Mapping[str, Any], ...]:
    return (
        _load(args.source_gate2_report),
        _load(args.source_gate3_report),
        _load(args.target_gate1_report),
    )


def _manifest(args: argparse.Namespace) -> int:
    gate2, gate3, gate1 = _reports(args)
    manifest = build_change_manifest(
        source_gate2_report=gate2,
        source_gate3_report=gate3,
        target_gate1_report=gate1,
        root=ROOT,
    )
    _write(args.output, manifest)
    print(
        f"impact={manifest['impact']}; changed_paths={len(manifest['changed_paths'])}; "
        f"review_subject_sha256={manifest['review_subject_sha256']}; "
        "status=PENDING_INDEPENDENT_REVIEW"
    )
    return 0


def _refresh_subject(args: argparse.Namespace) -> int:
    manifest = dict(_load(args.manifest))
    digest = manifest_review_subject_sha256(manifest)
    manifest["review_subject_sha256"] = digest
    _write(args.output or args.manifest, manifest)
    print(
        f"review_subject_sha256={digest}; set the approved review reference to "
        f"sha256:{digest} only after inspecting the exact diff"
    )
    return 0


def _report(args: argparse.Namespace) -> int:
    gate2, gate3, gate1 = _reports(args)
    report = build_carried_gate3_report(
        source_gate2_report=gate2,
        source_gate3_report=gate3,
        target_gate1_report=gate1,
        manifest=_load(args.manifest),
        root=ROOT,
    )
    _write(args.output, report)
    print(
        f"Gate-3 carry-forward {report['result']}; "
        f"impact={report['change_impact']}; "
        f"violations={len(report['invariant_violations'])}"
    )
    return 0 if report["result"] == "PASSED" else 1


def _validate(args: argparse.Namespace) -> int:
    report = _load(args.report)
    expected_commit = str(report.get("commit_sha") or "").strip().lower()
    violations = validate_carried_gate3_report(
        report,
        root=ROOT,
        expected_commit=expected_commit,
    )
    print(
        json.dumps(
            {
                "result": "PASSED" if not violations else "FAILED",
                "commit_sha": expected_commit,
                "invariant_violations": violations,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not violations else 1


def _add_report_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-gate2-report", type=Path, required=True)
    parser.add_argument("--source-gate3-report", type=Path, required=True)
    parser.add_argument("--target-gate1-report", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser(
        "manifest", help="build a PENDING exact-diff review manifest"
    )
    _add_report_arguments(manifest)
    manifest.add_argument("--output", type=Path, required=True)
    manifest.set_defaults(handler=_manifest)

    refresh = commands.add_parser(
        "refresh-review-subject",
        help="recompute the digest after an independent reviewer records assertions",
    )
    refresh.add_argument("--manifest", type=Path, required=True)
    refresh.add_argument("--output", type=Path)
    refresh.set_defaults(handler=_refresh_subject)

    report = commands.add_parser(
        "report", help="build the target-commit carried Gate-3 report"
    )
    _add_report_arguments(report)
    report.add_argument("--manifest", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.set_defaults(handler=_report)

    validate = commands.add_parser(
        "validate", help="revalidate a carried Gate-3 report against Git"
    )
    validate.add_argument("--report", type=Path, required=True)
    validate.set_defaults(handler=_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
