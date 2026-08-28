"""Gate-1 subprocess orchestration and JSON report construction."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from typing import Mapping, Sequence
import xml.etree.ElementTree as ET

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
    required_scenario_ids: Sequence[str] = tuple(REQUIRED_SCENARIO_IDS),
    required_group_minimums: Mapping[str, int] = REQUIRED_GROUP_MINIMUMS,
) -> dict:
    violations = [*activation_violations(activation), *test_violations]
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
    for scenario_id in sorted(set(required_scenario_ids) - present_ids):
        violations.append(
            {
                "property": "gate1_manifest_complete",
                "detail": f"required scenario is absent: {scenario_id}",
            }
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
        "schema_version": 1,
        "gate": "GATE_1_DETERMINISTIC_SIMULATION",
        "result": "PASSED" if not violations and pytest_exit_code == 0 else "FAILED",
        "commit_sha": commit_sha,
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


def run_gate1(*, root: Path, output: Path, model_seed: int) -> int:
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
    report = build_report(
        commit_sha=_git_sha(root),
        model_seed=model_seed,
        activation=activation,
        scenarios=scenarios,
        test_violations=test_violations,
        pytest_exit_code=completed.returncode,
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
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = args.output if args.output.is_absolute() else root / args.output
    return run_gate1(root=root, output=output, model_seed=args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
