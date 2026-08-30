"""Gate-1 subprocess orchestration and JSON report construction."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from gate1.contract import ACTIVATION_DEFAULTS, REQUIRED_POST_FAILURE_PROPERTIES
from gate1.manifest import (
    DEFAULT_MODEL_SEED,
    REQUIRED_GROUP_MINIMUMS,
    REQUIRED_SCENARIO_IDS,
    SCENARIO_GROUPS,
    unique_selectors,
)


def load_env_defaults(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_runtime_defaults(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("runtime defaults must be a JSON object")
    return {
        str(key): ("true" if value is True else "false" if value is False else str(value))
        for key, value in payload.items()
    }


def activation_snapshot(path: Path) -> dict[str, str]:
    values = (
        load_runtime_defaults(path)
        if path.suffix.lower() == ".json"
        else load_env_defaults(path)
    )
    return {key: values.get(key, "<missing>") for key in ACTIVATION_DEFAULTS}


def activation_violations(snapshot: Mapping[str, str]) -> list[dict[str, str]]:
    return [
        {
            "property": "production_activation_remains_disabled",
            "detail": f"{key}={snapshot.get(key)!r}, expected {expected!r}",
        }
        for key, expected in ACTIVATION_DEFAULTS.items()
        if str(snapshot.get(key, "")) != expected
    ]


def _group_for_classname(classname: str) -> str:
    normalized = str(classname or "").replace("\\", "/")
    for group in SCENARIO_GROUPS:
        if any(Path(selector).stem in normalized for selector in group.selectors):
            return group.group_id
    return "UNCLASSIFIED"


def parse_junit(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not path.exists():
        return [], [
            {
                "property": "gate1_test_process_completed",
                "detail": "pytest did not produce a JUnit report",
            }
        ]
    root = ET.parse(path).getroot()
    scenarios: list[dict[str, str]] = []
    violations: list[dict[str, str]] = []
    for case in root.iter("testcase"):
        classname = str(case.attrib.get("classname") or "")
        name = str(case.attrib.get("name") or "")
        scenario_id = f"{classname}::{name}" if classname else name
        status = "PASSED"
        detail = ""
        for tag, value in (("failure", "FAILED"), ("error", "ERROR"), ("skipped", "SKIPPED")):
            child = case.find(tag)
            if child is not None:
                status = value
                detail = str(child.attrib.get("message") or child.text or "").strip()
                break
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "group": _group_for_classname(classname),
                "result": status,
            }
        )
        if status in {"FAILED", "ERROR", "SKIPPED"}:
            violations.append(
                {
                    "property": "scenario_passed",
                    "detail": f"{scenario_id}: {detail[:1000]}",
                }
            )
    scenarios.sort(key=lambda item: item["scenario_id"])
    violations.sort(key=lambda item: item["detail"])
    return scenarios, violations


def build_report(
    *,
    commit_sha: str,
    model_seed: int,
    activation: Mapping[str, str],
    scenarios: Sequence[Mapping[str, str]],
    test_violations: Sequence[Mapping[str, str]],
    pytest_exit_code: int,
    source_identity: Mapping[str, object],
    ci_matrix: Sequence[Mapping[str, object]],
    required_scenario_ids: Sequence[str] = tuple(REQUIRED_SCENARIO_IDS),
    required_group_minimums: Mapping[str, int] = REQUIRED_GROUP_MINIMUMS,
) -> dict:
    violations = [*activation_violations(activation), *test_violations]
    commit_sha = str(commit_sha or "").strip().lower()
    if len(commit_sha) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in commit_sha
    ):
        violations.append(
            {
                "property": "exact_source_identity",
                "detail": "report commit is not a complete Git SHA",
            }
        )
    identity_commit = str(source_identity.get("commit_sha") or "").strip().lower()
    if identity_commit != commit_sha:
        violations.append(
            {
                "property": "exact_source_identity",
                "detail": "source identity commit does not match report commit",
            }
        )
    if source_identity.get("worktree_clean") is not True:
        violations.append(
            {
                "property": "exact_source_identity",
                "detail": "Git worktree contains tracked or untracked changes",
            }
        )
    for key in ("tracked_tree_sha256", "dependency_lock_sha256"):
        digest = str(source_identity.get(key) or "").strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            violations.append(
                {
                    "property": "exact_source_identity",
                    "detail": f"{key} is missing or is not a SHA-256 digest",
                }
            )

    required_python_versions = ("3.11", "3.12")
    ci_by_version = {
        str(item.get("python_version") or "").strip(): item for item in ci_matrix
    }
    for version in required_python_versions:
        evidence = ci_by_version.get(version)
        if evidence is None:
            violations.append(
                {
                    "property": "supported_python_ci_matrix_passed",
                    "detail": f"Python {version} CI evidence is absent",
                }
            )
            continue
        if (
            str(evidence.get("result") or "").upper() != "PASSED"
            or str(evidence.get("commit_sha") or "").strip().lower() != commit_sha
        ):
            violations.append(
                {
                    "property": "supported_python_ci_matrix_passed",
                    "detail": (
                        f"Python {version} CI did not pass for exact commit {commit_sha}"
                    ),
                }
            )
    if pytest_exit_code != 0 and not test_violations:
        violations.append(
            {
                "property": "gate1_test_process_completed",
                "detail": f"pytest exited with code {pytest_exit_code}",
            }
        )
    counts: dict[str, int] = {group.group_id: 0 for group in SCENARIO_GROUPS}
    counts["UNCLASSIFIED"] = 0
    for scenario in scenarios:
        group = str(scenario.get("group") or "UNCLASSIFIED")
        counts[group] = counts.get(group, 0) + 1
    if counts["UNCLASSIFIED"]:
        violations.append(
            {
                "property": "gate1_manifest_complete",
                "detail": f"{counts['UNCLASSIFIED']} selected scenario(s) were unclassified",
            }
        )
    present_ids = {str(item.get("scenario_id") or "") for item in scenarios}
    violations.extend(
        {
            "property": "gate1_manifest_complete",
            "detail": f"required scenario is absent: {scenario_id}",
        }
        for scenario_id in sorted(set(required_scenario_ids) - present_ids)
    )
    for group_id, minimum in sorted(required_group_minimums.items()):
        actual = counts.get(group_id, 0)
        if actual < int(minimum):
            violations.append(
                {
                    "property": "gate1_manifest_complete",
                    "detail": (
                        f"{group_id} contains {actual} scenario(s); "
                        f"minimum is {int(minimum)}"
                    ),
                }
            )
    return {
        "schema_version": 2,
        "gate": "GATE_1_DETERMINISTIC_SIMULATION",
        "result": "PASSED" if not violations and pytest_exit_code == 0 else "FAILED",
        "commit_sha": commit_sha,
        "source_identity": dict(source_identity),
        "supported_python_versions": list(required_python_versions),
        "ci_matrix": [dict(item) for item in ci_matrix],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "model_seeds": [int(model_seed)],
        "scenario_count": len(scenarios),
        "scenario_counts_by_group": counts,
        "required_scenario_ids": sorted(set(required_scenario_ids)),
        "required_group_minimums": dict(required_group_minimums),
        "scenarios": list(scenarios),
        "required_post_failure_properties": list(REQUIRED_POST_FAILURE_PROPERTIES),
        "invariant_violations": violations,
        "activation_defaults": dict(activation),
        "production_activation_authorized": False,
        "pytest_exit_code": int(pytest_exit_code),
    }


def _git_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_tree_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_worktree_clean(root: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return not bool(result.stdout.strip())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tracked_tree_sha256(root: Path) -> str:
    """Hash current bytes and paths for every Git-tracked file."""

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    digest = hashlib.sha256()
    for raw_path in sorted(filter(None, result.stdout.split(b"\0"))):
        relative_path = raw_path.decode("utf-8", errors="surrogateescape")
        path = root / relative_path
        content = path.read_bytes()
        digest.update(len(raw_path).to_bytes(8, "big"))
        digest.update(raw_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def source_identity(root: Path) -> dict[str, object]:
    lock_path = root / "requirements.lock"
    return {
        "commit_sha": _git_sha(root),
        "head_tree_sha": _git_tree_sha(root),
        "worktree_clean": _git_worktree_clean(root),
        "tracked_tree_sha256": _tracked_tree_sha256(root),
        "dependency_lock_path": "requirements.lock",
        "dependency_lock_sha256": _sha256_file(lock_path),
    }


def load_ci_matrix(path: Path | None) -> list[dict[str, object]]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("ci_matrix") if isinstance(payload, dict) else payload
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError("CI evidence must be a list or an object containing ci_matrix")
    return [dict(item) for item in records]


def run_gate1(
    *,
    root: Path,
    output: Path,
    model_seed: int,
    ci_evidence: Path | None = None,
) -> int:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONHASHSEED": "0",
            "QT_QPA_PLATFORM": "offscreen",
            "GATE1_MODEL_SEED": str(int(model_seed)),
        }
    )
    with tempfile.TemporaryDirectory(prefix="quant-app-gate1-") as temp_dir:
        junit_path = Path(temp_dir) / "gate1-junit.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            *unique_selectors(),
            "-q",
            f"--junitxml={junit_path}",
        ]
        completed = subprocess.run(command, cwd=root, env=env, check=False)
        scenarios, test_violations = parse_junit(junit_path)

    activation = activation_snapshot(root / "config" / "runtime.json")
    identity = source_identity(root)
    report = build_report(
        commit_sha=str(identity["commit_sha"]),
        model_seed=model_seed,
        activation=activation,
        scenarios=scenarios,
        test_violations=test_violations,
        pytest_exit_code=completed.returncode,
        source_identity=identity,
        ci_matrix=load_ci_matrix(ci_evidence),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix(output.suffix + ".tmp")
    temporary_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_output.replace(output)
    print(
        f"Gate 1 {report['result']}: {report['scenario_count']} scenarios; "
        f"report={output}"
    )
    return 0 if report["result"] == "PASSED" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic WS7 Gate-1 suite")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/gate1_report.json"),
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_MODEL_SEED)
    parser.add_argument(
        "--ci-evidence",
        type=Path,
        help="JSON evidence for passing Python 3.11 and 3.12 CI jobs",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = args.output if args.output.is_absolute() else root / args.output
    ci_evidence = args.ci_evidence
    if ci_evidence is not None and not ci_evidence.is_absolute():
        ci_evidence = root / ci_evidence
    return run_gate1(
        root=root,
        output=output,
        model_seed=args.seed,
        ci_evidence=ci_evidence,
    )


if __name__ == "__main__":
    raise SystemExit(main())
