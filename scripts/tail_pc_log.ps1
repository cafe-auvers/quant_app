<#
Run this on the LAPTOP, any time after setup_pc_winrm_tailscale_access.ps1
and setup_laptop_winrm_trust.ps1 have both been run once.

Streams a log file live from the always-on PC's data\logs folder over
WinRM/Tailscale, same as watching it locally with `Get-Content -Wait`.
Caches the credential for the PowerShell session (via -Credential) so you
aren't prompted on every call within the same window.

Usage:
    .\scripts\tail_pc_log.ps1
    .\scripts\tail_pc_log.ps1 -LogName pc_morning_routine.log
    .\scripts\tail_pc_log.ps1 -LogName quant_app.log -Lines 200
    $cred = Get-Credential DESKTOP-E42GSKJ\<pc-username>
    .\scripts\tail_pc_log.ps1 -Credential $cred   # skips the prompt
#>

param(
    [string]$LogName = "quant_app.log",
    [int]$Lines = 50,
    [string]$PcTailscaleIp,
    [string]$PcRepoRoot = "$env:USERPROFILE\Documents\quant_app",  # the PC's clone path -- adjust if it lives elsewhere there
    [System.Management.Automation.PSCredential]$Credential
)

$ErrorActionPreference = "Stop"

if (-not $PcTailscaleIp) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
    $EnvFile = Join-Path $RepoRoot ".env"
    if (Test-Path $EnvFile) {
        $line = Get-Content $EnvFile | Where-Object { $_ -match '^\s*PC_REMOTE_CONTROL_HOST\s*=' } | Select-Object -First 1
        if ($line) { $PcTailscaleIp = ($line -split '=', 2)[1].Trim() }
    }
}
if (-not $PcTailscaleIp) {
    throw "Couldn't determine the PC's Tailscale IP. Pass it explicitly: -PcTailscaleIp 100.x.x.x"
}

if (-not $Credential) {
    $Credential = Get-Credential -Message "PC Windows account (e.g. DESKTOP-E42GSKJ\<username>) for $PcTailscaleIp"
}

Write-Host "Tailing $LogName on $PcTailscaleIp -- Ctrl+C to stop."
Invoke-Command -ComputerName $PcTailscaleIp -Credential $Credential -ScriptBlock {
    param($RepoRoot, $LogName, $Lines)
    $Path = Join-Path $RepoRoot "data\logs\$LogName"
    if (-not (Test-Path $Path)) {
        Write-Error "No such log file on the PC: $Path"
        return
    }
    Get-Content -Path $Path -Tail $Lines -Wait
} -ArgumentList $PcRepoRoot, $LogName, $Lines
