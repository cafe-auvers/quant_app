"""Build a draft, fail-closed Gate-4 change-impact manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activation_gates.evidence import canonical_report_sha256  # noqa: E402
from activation_gates.requalification import (  # noqa: E402
    gate4_change_impact,
    normalized_changed_paths,
    required_gate4_supervised_sessions,
)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{path} must contain a JSON object")
    return value


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build_manifest(baseline_report: Mapping[str, Any]) -> dict[str, Any]:
    if (
        baseline_report.get("gate") != "GATE_4_CONTROLLED_LIVE"
        or baseline_report.get("result") != "PASSED"
    ):
        raise RuntimeError("baseline must be a PASSED Gate-4 report")
    baseline_commit = str(baseline_report.get("commit_sha") or "").strip().lower()
    target_commit = _git("rev-parse", "HEAD").stdout.strip().lower()
    if _git("status", "--porcelain").stdout.strip():
        raise RuntimeError("build the manifest from a clean target worktree")
    if _git(
        "merge-base", "--is-ancestor", baseline_commit, target_commit, check=False
    ).returncode != 0:
        raise RuntimeError("baseline commit must be an ancestor of the target")
    changed_paths = normalized_changed_paths(
        _git("diff", "--name-only", baseline_commit, target_commit).stdout.splitlines()
    )
    if not changed_paths:
        raise RuntimeError("baseline and target commits have no changed paths")
    impact = gate4_change_impact(changed_paths)
    return {
        "schema_version": 1,
        "gate": "GATE_4_CONTROLLED_LIVE",
        "baseline_commit_sha": baseline_commit,
        "target_commit_sha": target_commit,
        "baseline_gate4_report_sha256": canonical_report_sha256(baseline_report),
        "changed_paths": list(changed_paths),
        "impact": impact,
        "required_supervised_sessions": required_gate4_supervised_sessions(impact),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "review": {
            "status": "PENDING",
            "author": "",
            "reviewer": "",
            "reviewed_at": "",
            "reference": "",
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a draft Gate-4 requalification change-impact manifest"
    )
    parser.add_argument("--baseline-gate4-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = build_manifest(_load(args.baseline_gate4_report))
    _write(args.output, manifest)
    print(
        f"Gate-4 impact={manifest['impact']}; "
        f"required_sessions={manifest['required_supervised_sessions']}; "
        "independent review remains PENDING"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
