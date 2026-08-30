<#
Run a PowerShell command on the always-on PC from the laptop over
WinRM/Tailscale. By default this loads the DPAPI-encrypted credential created
by save_laptop_pc_winrm_credential.ps1, so no password prompt is required.

Usage:
    .\scripts\invoke_pc_command.ps1 -ScriptBlock { $env:COMPUTERNAME }
    .\scripts\invoke_pc_command.ps1 -ScriptBlock { Get-Process python }
#>

param(
    [Parameter(Mandatory = $true)]
    [scriptblock]$ScriptBlock,
    [object[]]$ArgumentList = @(),
    [string]$PcTailscaleIp,
    [System.Management.Automation.PSCredential]$Credential,
    [string]$CredentialPath = (Join-Path $env:LOCALAPPDATA "quant_app\pc_winrm_credential.clixml")
)

$ErrorActionPreference = "Stop"

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

if (-not $Credential) {
    if (-not (Test-Path -LiteralPath $CredentialPath)) {
        throw "No saved PC credential exists. Run .\scripts\save_laptop_pc_winrm_credential.ps1 once from the laptop first."
    }
    try {
        $Credential = [System.Management.Automation.PSCredential](
            Import-Clixml -LiteralPath $CredentialPath
        )
    } catch {
        throw "The saved PC credential cannot be decrypted by this Windows user on this laptop. Re-run .\scripts\save_laptop_pc_winrm_credential.ps1. $($_.Exception.Message)"
    }
}

if ($ArgumentList.Count -gt 0) {
    Invoke-Command -ComputerName $PcTailscaleIp -Authentication Negotiate `
        -Credential $Credential `
        -ScriptBlock $ScriptBlock -ArgumentList $ArgumentList
} else {
    Invoke-Command -ComputerName $PcTailscaleIp -Authentication Negotiate `
        -Credential $Credential `
        -ScriptBlock $ScriptBlock
}
