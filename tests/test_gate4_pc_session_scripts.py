from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "scripts" / "start_gate4_pc_session.ps1"
CLOSE = ROOT / "scripts" / "close_gate4_pc_session.ps1"
DISARM = ROOT / "scripts" / "disarm_gate4_session.py"
MAIN_WINDOW = ROOT / "src" / "ui" / "main_window.py"


def test_gate4_start_refuses_active_runtime_before_any_mutation():
    text = START.read_text(encoding="utf-8")
    try_body = text.index('Write-Gate4Log "Gate 4 supervised start beginning"')
    guard = text.index("Assert-NoActiveGate4Runtime", try_body)
    disable = text.index("Set-Gate4Collection $false", guard)
    disarm = text.index("disarm_gate4_session.py", disable)
    stop = text.index("Stop-ExistingDashboard", disarm)

    assert guard < disable < disarm < stop
    assert "Refusing to replace an active Gate-4 dashboard" in text
    assert "if ($collectionConfiguredByThisRun)" in text


def test_internal_worker_restarts_preserve_open_gate4_session():
    text = MAIN_WINDOW.read_text(encoding="utf-8")

    assert text.count("worker.request_stop(finalize_gate4_session=False)") == 2


def test_gate4_close_accepts_absent_dashboard_only_with_clean_evidence():
    text = CLOSE.read_text(encoding="utf-8")

    assert 'if (-not $window) { return $false }' in text
    assert "Dashboard already absent; verifying prior clean session closure" in text
    closure_check = text.index(
        "Final reconciliation or clean SESSION_ENDED was not recorded"
    )
    owner_release = text.index(
        "Execution ownership was not released after controlled close"
    )
    assert closure_check < owner_release


def test_disarm_still_turns_shared_control_off_after_session_already_closed():
    text = DISARM.read_text(encoding="utf-8")
    closed_check = text.index("session_already_closed = any(")
    control_off = text.index("result = set_live_trading_control(", closed_check)
    evidence_append = text.index(
        "if collector is not None and not session_already_closed:", control_off
    )

    assert closed_check < control_off < evidence_append
    assert "return 0" not in text[closed_check:control_off]
