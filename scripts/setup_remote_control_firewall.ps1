<#
Run this on the always-on PC as Administrator, after Tailscale is installed
and signed in there.

Opens Windows Firewall for the remote-control listener (see
scripts/pc_remote_control_listener.py) on the Tailscale virtual network
adapter specifically -- not a broad IP range. Only traffic that already
passed through Tailscale's own mesh authentication can reach this port, so
this stays as narrowly scoped as the MySQL Tailscale rule from
setup_mysql_tailscale_access.ps1.

Usage:
    .\scripts\setup_remote_control_firewall.ps1
#>

$ErrorActionPreference = "Stop"

$Port = if ($env:REMOTE_CONTROL_PORT) { $env:REMOTE_CONTROL_PORT } else { 47821 }

$adapter = Get-NetAdapter | Where-Object { $_.InterfaceDescription -match "Tailscale" -or $_.Name -match "Tailscale" } | Select-Object -First 1
if (-not $adapter) {
    throw "No Tailscale network adapter found. Make sure Tailscale is installed and you've signed in at least once, then re-run this script."
}

$RuleName = "PC Remote Control (quant_app)"
if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort $Port `
        -InterfaceAlias $adapter.Name -Action Allow | Out-Null
    Write-Host "Firewall rule '$RuleName' created (TCP $Port, inbound, Tailscale adapter '$($adapter.Name)' only)."
} else {
    Write-Host "Firewall rule '$RuleName' already exists -- skipping."
}

Write-Host ""
Write-Host "Remaining manual step -- set a shared secret token in .env on BOTH machines:"
Write-Host "  REMOTE_CONTROL_TOKEN=<pick a long random string, same value on PC and laptop>"
Write-Host "  REMOTE_CONTROL_PORT=$Port   (optional; only needed if you changed it from the default 47821)"
Write-Host ""
Write-Host "The listener itself is launched by pc_morning_routine.ps1 alongside main.py -- nothing else to start manually."
