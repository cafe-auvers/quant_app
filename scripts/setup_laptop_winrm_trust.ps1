<#
Run this on the LAPTOP as Administrator, AFTER
setup_pc_winrm_tailscale_access.ps1 has been run on the always-on PC.

WinRM authenticates the remote machine's identity using Kerberos by
default, which only works inside a shared Windows domain. This PC and
laptop are just two workgroup machines connected over Tailscale, so instead
the laptop must explicitly mark the PC's Tailscale IP as a trusted WinRM
target (NTLM auth) -- the WinRM equivalent of the MySQL/remote-control
Tailscale firewall scoping already used elsewhere in this project, just
expressed as a client-side trust setting instead of a firewall rule.

This adds to any existing TrustedHosts entries rather than overwriting
them.

Usage:
    .\scripts\setup_laptop_winrm_trust.ps1
    .\scripts\setup_laptop_winrm_trust.ps1 -PcTailscaleIp 100.x.x.x   # override .env's PC_REMOTE_CONTROL_HOST
#>

param(
    [string]$PcTailscaleIp
)

$ErrorActionPreference = "Stop"

$IsElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsElevated) {
    throw "Run this from an elevated (Administrator) PowerShell window -- changing the WinRM client TrustedHosts list requires it. Right-click PowerShell -> Run as administrator, then re-run this command."
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
    throw "Couldn't determine the PC's Tailscale IP. Pass it explicitly: .\scripts\setup_laptop_winrm_trust.ps1 -PcTailscaleIp 100.x.x.x"
}

# Make sure the WinRM client service itself is running on the laptop before
# touching its config.
if ((Get-Service WinRM).Status -ne "Running") {
    Start-Service WinRM
}

$existing = (Get-Item WSMan:\localhost\Client\TrustedHosts).Value
$hosts = @()
if ($existing) { $hosts = $existing -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ } }

if ($hosts -contains $PcTailscaleIp -or $hosts -contains "*") {
    Write-Host "TrustedHosts already includes $PcTailscaleIp (or '*') -- skipping."
} else {
    $hosts += $PcTailscaleIp
    Set-Item WSMan:\localhost\Client\TrustedHosts -Value ($hosts -join ',') -Force
    Write-Host "TrustedHosts updated: $($hosts -join ', ')"
}

Write-Host ""
Write-Host "Test the connection (replace the account with the one printed by setup_pc_winrm_tailscale_access.ps1):"
Write-Host "    `$cred = Get-Credential <PC-HOSTNAME>\<pc-username>"
Write-Host "    Invoke-Command -ComputerName $PcTailscaleIp -Credential `$cred -ScriptBlock { `$env:COMPUTERNAME }"
Write-Host ""
Write-Host "Once that works, tail a PC log live with:"
Write-Host "    .\scripts\tail_pc_log.ps1 -LogName quant_app.log -Credential `$cred"
