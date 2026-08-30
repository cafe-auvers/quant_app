<#
Run this once on the LAPTOP from the normal Windows account that will access
the PC. Do not run it as a different administrator account.

The password is collected by Get-Credential, verified against the PC over
WinRM/Tailscale, and then exported with Windows DPAPI encryption. The saved
file can be decrypted only by this Windows user on this laptop. It is stored
under LOCALAPPDATA, never in the repository or an environment file.

Prerequisites:
    1. Run setup_pc_winrm_tailscale_access.ps1 on the PC as Administrator.
    2. Run setup_laptop_winrm_trust.ps1 on the laptop as Administrator.

Usage:
    .\scripts\save_laptop_pc_winrm_credential.ps1
    .\scripts\save_laptop_pc_winrm_credential.ps1 `
        -PcTailscaleIp 100.x.x.x `
        -PcWindowsUser <PC-HOSTNAME>\<pc-username>

Re-run this command after the PC account password changes.
#>

param(
    [string]$PcTailscaleIp,
    [string]$PcWindowsUser,
    [string]$CredentialPath = (Join-Path $env:LOCALAPPDATA "quant_app\pc_winrm_credential.clixml")
)

$ErrorActionPreference = "Stop"

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "Saved WinRM credentials are supported only on Windows because Export-Clixml relies on Windows DPAPI encryption."
}

if (-not $PcTailscaleIp) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
    foreach ($ConfigName in @("runtime.local.json", "runtime.json")) {
        $ConfigFile = Join-Path $RepoRoot "config\$ConfigName"
        if (Test-Path $ConfigFile) {
            try {
                $RuntimeConfig = Get-Content $ConfigFile -Raw | ConvertFrom-Json
                $Candidate = [string]$RuntimeConfig.PC_REMOTE_CONTROL_HOST
                if ($Candidate.Trim()) {
                    $PcTailscaleIp = $Candidate.Trim()
                    break
                }
            } catch {
                throw "Couldn't parse ${ConfigFile}: $($_.Exception.Message)"
            }
        }
    }
}
if (-not $PcTailscaleIp) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
    $EnvFile = Join-Path $RepoRoot ".env"
    if (Test-Path $EnvFile) {
        $line = Get-Content $EnvFile | Where-Object { $_ -match '^\s*PC_REMOTE_CONTROL_HOST\s*=' } | Select-Object -First 1
        if ($line) { $PcTailscaleIp = ($line -split '=', 2)[1].Trim() }
    }
}
if (-not $PcTailscaleIp) {
    throw "Couldn't determine the PC's Tailscale IP. Pass it explicitly with -PcTailscaleIp 100.x.x.x."
}

$credentialPrompt = @{
    Message = "PC Windows account for WinRM access to $PcTailscaleIp"
}
if ($PcWindowsUser) {
    $credentialPrompt.UserName = $PcWindowsUser
}
$credential = Get-Credential @credentialPrompt
if (-not $credential) {
    throw "Credential entry was cancelled; nothing was saved."
}

Write-Host "Verifying the credential against $PcTailscaleIp before saving it ..."
$remoteIdentity = Invoke-Command -ComputerName $PcTailscaleIp `
    -Authentication Negotiate -Credential $credential -ScriptBlock {
    "$env:COMPUTERNAME\$env:USERNAME"
}
if (-not $remoteIdentity) {
    throw "The PC did not return a Windows identity; nothing was saved."
}
$remoteComputerName = ([string]$remoteIdentity -split '\\', 2)[0]
if ($remoteComputerName -ieq $env:COMPUTERNAME) {
    throw "This command is running on the PC itself. Run it from the laptop's normal Windows account so DPAPI binds the credential to the laptop."
}

$credentialDirectory = Split-Path -Parent $CredentialPath
New-Item -ItemType Directory -Path $credentialDirectory -Force | Out-Null
$temporaryCredentialPath = Join-Path $credentialDirectory (
    ".pc_winrm_credential.$PID.$([guid]::NewGuid().ToString('N')).tmp"
)
try {
    $credential | Export-Clixml -LiteralPath $temporaryCredentialPath -Force

    # Import and use the staged file before replacing a previously valid
    # credential. Casting preserves compatibility between Windows PowerShell
    # and newer PowerShell releases.
    $savedCredential = [System.Management.Automation.PSCredential](
        Import-Clixml -LiteralPath $temporaryCredentialPath
    )
    if (-not $savedCredential -or $savedCredential.UserName -ne $credential.UserName) {
        throw "The staged credential could not be read back correctly."
    }
    $savedRemoteIdentity = Invoke-Command -ComputerName $PcTailscaleIp `
        -Authentication Negotiate -Credential $savedCredential -ScriptBlock {
        "$env:COMPUTERNAME\$env:USERNAME"
    }
    if ([string]$savedRemoteIdentity -cne [string]$remoteIdentity) {
        throw "The staged credential returned an unexpected remote identity."
    }

    Move-Item -LiteralPath $temporaryCredentialPath -Destination $CredentialPath -Force
} finally {
    if (Test-Path -LiteralPath $temporaryCredentialPath) {
        Remove-Item -LiteralPath $temporaryCredentialPath -Force
    }
}

Write-Host "Saved the verified credential for $remoteIdentity."
Write-Host "Encrypted credential file: $CredentialPath"
Write-Host "It is usable only by $env:USERDOMAIN\$env:USERNAME on this laptop."
Write-Host "Future tail_pc_log.ps1 and invoke_pc_command.ps1 calls will use it automatically."
