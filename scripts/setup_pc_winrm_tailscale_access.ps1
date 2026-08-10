<#
Run this on the always-on PC as Administrator, after Tailscale is installed
and signed in there.

Enables PowerShell Remoting (WinRM) so the laptop can run commands on this
PC over Tailscale -- reading/tailing log files today, and (in a later step)
launching scripts remotely. Traffic stays on the Tailscale virtual adapter
only, same scoping pattern as setup_mysql_tailscale_access.ps1 and
setup_remote_control_firewall.ps1. WinRM itself talks plain HTTP (port
5985); that's fine here because Tailscale already encrypts everything on
that adapter (WireGuard), the same trust model already used for MySQL over
Tailscale in this project.

Unlike the token-gated pc_remote_control_listener.py, WinRM authenticates
with a real Windows account and password on this PC and then allows
arbitrary PowerShell execution as that user -- treat that account's
password with the same care as any other admin credential.

Usage:
    .\scripts\setup_pc_winrm_tailscale_access.ps1
#>

$ErrorActionPreference = "Stop"

# New-NetFirewallRule's access-denied failure comes through as a non-terminating
# CIM error that $ErrorActionPreference = "Stop" does not reliably catch, so an
# unelevated run can print a false "created" success message. Check explicitly
# up front instead of trusting that a failed call will stop the script.
$IsElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsElevated) {
    throw "Run this from an elevated (Administrator) PowerShell window -- enabling WinRM and creating a firewall rule requires it. Right-click PowerShell -> Run as administrator, then re-run this command."
}

Write-Host "Enabling PowerShell Remoting (WinRM) ..."
Enable-PSRemoting -Force -SkipNetworkProfileCheck | Out-Null
Write-Host "PowerShell Remoting enabled."

# Enable-PSRemoting's own firewall rule ("Windows Remote Management (HTTP-In)")
# is scoped to the Private/Domain profiles by default. The Tailscale adapter
# is typically categorized Public, so it needs its own explicit rule scoped
# to that adapter specifically -- same reasoning as the MySQL/remote-control
# Tailscale rules.
$adapter = Get-NetAdapter | Where-Object { $_.InterfaceDescription -match "Tailscale" -or $_.Name -match "Tailscale" } | Select-Object -First 1
if (-not $adapter) {
    throw "No Tailscale network adapter found. Make sure Tailscale is installed and you've signed in at least once, then re-run this script."
}

$RuleName = "WinRM Tailscale (quant_app)"
if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort 5985 `
        -InterfaceAlias $adapter.Name -Action Allow -ErrorAction Stop | Out-Null
    if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
        throw "New-NetFirewallRule did not report an error, but the rule doesn't exist afterward -- something silently failed. Try again from an elevated window."
    }
    Write-Host "Firewall rule '$RuleName' created (TCP 5985, inbound, Tailscale adapter '$($adapter.Name)' only)."
} else {
    Write-Host "Firewall rule '$RuleName' already exists -- skipping."
}

$tailscaleIp = (& tailscale ip -4 2>$null)
$thisUser = "$env:COMPUTERNAME\$env:USERNAME"

Write-Host ""
Write-Host "This PC's Tailscale IP: $tailscaleIp"
Write-Host "This PC's Windows account for remoting: $thisUser"
Write-Host ""
Write-Host "Remaining manual step -- on the LAPTOP, trust this PC's Tailscale IP as a WinRM client"
Write-Host "(required because these two machines aren't in the same Windows domain):"
Write-Host "    .\scripts\setup_laptop_winrm_trust.ps1"
Write-Host ""
Write-Host "Then from the laptop, test with:"
Write-Host "    `$cred = Get-Credential $thisUser"
Write-Host "    Invoke-Command -ComputerName $tailscaleIp -Credential `$cred -ScriptBlock { `$env:COMPUTERNAME }"
