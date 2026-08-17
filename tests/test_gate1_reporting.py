from __future__ import annotations

import json
from pathlib import Path

from gate1.contract import ACTIVATION_DEFAULTS
from gate1.reporting import activation_snapshot, build_report, parse_junit


def test_gate1_report_records_scenarios_seed_commit_and_closed_activation(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version='1.0' encoding='utf-8'?>
<testsuites><testsuite tests="2"><testcase classname="tests.test_gate1_capstone" name="test_a"/><testcase classname="tests.test_kis_websocket" name="test_b"/></testsuite></testsuites>
""",
        encoding="utf-8",
    )
    scenarios, violations = parse_junit(junit)
    report = build_report(
        commit_sha="a" * 40,
        model_seed=17,
        activation=ACTIVATION_DEFAULTS,
        scenarios=scenarios,
        test_violations=violations,
        pytest_exit_code=0,
    )

    assert report["result"] == "PASSED"
    assert report["commit_sha"] == "a" * 40
    assert report["model_seeds"] == [17]
    assert report["scenario_count"] == 2
    assert report["scenario_counts_by_group"]["PR8_CROSS_WORKSTREAM"] == 1
    assert report["scenario_counts_by_group"]["F2_WEBSOCKET_PROTOCOL"] == 1
    assert report["invariant_violations"] == []
    assert report["production_activation_authorized"] is False
    json.dumps(report)


def test_gate1_report_fails_when_a_production_default_opens():
    activation = dict(ACTIVATION_DEFAULTS)
    activation["TRADING_ENABLED"] = "true"
    report = build_report(
        commit_sha="b" * 40,
        model_seed=1,
        activation=activation,
        scenarios=[],
        test_violations=[],
        pytest_exit_code=0,
    )

    assert report["result"] == "FAILED"
    assert report["invariant_violations"][0]["property"] == (
        "production_activation_remains_disabled"
    )


def test_repository_activation_snapshot_matches_gate1_contract():
    root = Path(__file__).resolve().parents[1]
    assert activation_snapshot(root / ".env.example") == dict(ACTIVATION_DEFAULTS)
