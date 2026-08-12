<#
Run this on the always-on PC as Administrator, AFTER Tailscale is installed
and signed in there.

Opens Windows Firewall for MySQL on the Tailscale virtual network adapter
specifically -- not a broad IP range. Only traffic that already passed
through Tailscale's own mesh authentication can arrive on that adapter, so
this stays just as narrowly scoped in spirit as the LAN-only rule from
setup_mysql_lan_access.ps1, just extended to cover the VPN path too. Both
rules coexist: LAN access still works at home, Tailscale access works
anywhere.

Usage:
    .\scripts\setup_mysql_tailscale_access.ps1
#>

$ErrorActionPreference = "Stop"

$adapter = Get-NetAdapter | Where-Object { $_.InterfaceDescription -match "Tailscale" -or $_.Name -match "Tailscale" } | Select-Object -First 1
if (-not $adapter) {
    throw "No Tailscale network adapter found. Make sure Tailscale is installed and you've signed in at least once, then re-run this script."
}

$RuleName = "MySQL Tailscale (quant_app)"
if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort 3306 `
        -InterfaceAlias $adapter.Name -Action Allow | Out-Null
    Write-Host "Firewall rule '$RuleName' created (TCP 3306, inbound, Tailscale adapter '$($adapter.Name)' only)."
} else {
    Write-Host "Firewall rule '$RuleName' already exists -- skipping."
}

$tailscaleIp = (& tailscale ip -4 2>$null)
Write-Host ""
Write-Host "This PC's Tailscale IP: $tailscaleIp"
Write-Host ""
Write-Host "Remaining manual step -- grant the laptop's Tailscale IP access in MySQL:"
Write-Host "  1. On the laptop, run:  tailscale ip -4   (note the 100.x.x.x address)"
Write-Host "  2. On this PC:  mysql -u root -p"
Write-Host "  3. Run (replace <laptop-tailscale-ip> and choose your own password):"
Write-Host "       CREATE USER 'quant_remote'@'<laptop-tailscale-ip>' IDENTIFIED BY 'choose-a-strong-password';"
Write-Host "       GRANT ALL PRIVILEGES ON quant_app.* TO 'quant_remote'@'<laptop-tailscale-ip>';"
Write-Host "       FLUSH PRIVILEGES;"
Write-Host "     (This is a SEPARATE grant from the existing 192.168.x.% one -- both coexist,"
Write-Host "      so LAN access at home and Tailscale access away both keep working.)"
