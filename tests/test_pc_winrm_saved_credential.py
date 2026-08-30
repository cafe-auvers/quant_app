from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAVE_SCRIPT = ROOT / "scripts" / "save_laptop_pc_winrm_credential.ps1"
TAIL_SCRIPT = ROOT / "scripts" / "tail_pc_log.ps1"
INVOKE_SCRIPT = ROOT / "scripts" / "invoke_pc_command.ps1"
PC_SETUP_SCRIPT = ROOT / "scripts" / "setup_pc_winrm_tailscale_access.ps1"
LAPTOP_SETUP_SCRIPT = ROOT / "scripts" / "setup_laptop_winrm_trust.ps1"


def test_saved_pc_credential_is_verified_before_dpapi_export():
    text = SAVE_SCRIPT.read_text(encoding="utf-8")

    verify = text.index("Invoke-Command -ComputerName")
    export = text.index("Export-Clixml -LiteralPath")

    assert verify < export
    assert "$env:LOCALAPPDATA" in text
    assert "Export-Clixml relies on Windows DPAPI encryption" in text
    assert "This command is running on the PC itself" in text


def test_staged_credential_is_retested_before_atomic_replacement():
    text = SAVE_SCRIPT.read_text(encoding="utf-8")

    export = text.index("Export-Clixml -LiteralPath $temporaryCredentialPath")
    staged_import = text.index("Import-Clixml -LiteralPath $temporaryCredentialPath")
    staged_test = text.index("$savedRemoteIdentity = Invoke-Command")
    replacement = text.index("Move-Item -LiteralPath $temporaryCredentialPath")

    assert export < staged_import < staged_test < replacement
    assert "Remove-Item -LiteralPath $temporaryCredentialPath" in text
    assert text.count("-Authentication Negotiate") >= 2


def test_remote_helpers_load_saved_credential_without_a_password_prompt():
    for script in (TAIL_SCRIPT, INVOKE_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "Import-Clixml -LiteralPath $CredentialPath" in text
        assert "Get-Credential" not in text
        assert "$env:LOCALAPPDATA" in text


def test_log_tail_resolves_the_repository_on_the_remote_pc():
    text = TAIL_SCRIPT.read_text(encoding="utf-8")

    assert "[string]$PcRepoRoot," in text
    assert 'Join-Path $env:USERPROFILE "quant_app"' in text
    assert 'Join-Path $env:USERPROFILE "Documents\\quant_app"' in text
    assert "Pass -PcRepoRoot explicitly" in text


def test_saved_credential_file_is_outside_the_repository():
    text = SAVE_SCRIPT.read_text(encoding="utf-8")

    assert '"quant_app\\pc_winrm_credential.clixml"' in text
    assert "Export-Clixml -LiteralPath $temporaryCredentialPath" in text
    assert "Move-Item -LiteralPath $temporaryCredentialPath" in text


def test_pc_setup_hands_off_to_the_one_time_laptop_credential_save():
    text = PC_SETUP_SCRIPT.read_text(encoding="utf-8")

    assert "save_laptop_pc_winrm_credential.ps1" in text
    assert "-PcWindowsUser $thisUser" in text
    assert "Get-Credential" not in text


def test_laptop_winrm_scripts_prefer_runtime_config_before_legacy_env():
    for script in (SAVE_SCRIPT, TAIL_SCRIPT, INVOKE_SCRIPT, LAPTOP_SETUP_SCRIPT):
        text = script.read_text(encoding="utf-8")
        runtime_config = text.index('runtime.local.json')
        legacy_env = text.index('Join-Path $RepoRoot ".env"')

        assert runtime_config < legacy_env
        assert "PC_REMOTE_CONTROL_HOST" in text
