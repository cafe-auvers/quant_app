<#
Run this on the always-on PC (the one running MySQL) as Administrator, from
a PowerShell prompt opened in the repo root.

Opens Windows Firewall for MySQL to the local subnet only. The my.ini edit
and MySQL user grant are printed as manual steps below rather than scripted,
since they need your MySQL root password and that shouldn't be stored or
piped through a script.

Usage:
    .\scripts\setup_mysql_lan_access.ps1
#>

$ErrorActionPreference = "Stop"

$RuleName = "MySQL LAN (quant_app)"
if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort 3306 -RemoteAddress LocalSubnet -Action Allow | Out-Null
    Write-Host "Firewall rule '$RuleName' created (TCP 3306, inbound, LocalSubnet only)."
} else {
    Write-Host "Firewall rule '$RuleName' already exists -- skipping."
}

$PcIp = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp,Manual -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "169.254.*" } | Select-Object -First 1 -ExpandProperty IPAddress)
if (-not $PcIp) { $PcIp = "<run ipconfig to find this PC's IPv4 address>" }
$Subnet = if ($PcIp -match '^\d+\.\d+\.\d+\.') { "$($Matches[0])%" } else { "<subnet>.%" }

Write-Host ""
Write-Host "This PC's LAN IP looks like: $PcIp"
Write-Host "(Give this PC a static IP or a DHCP reservation in your router, since the laptop's .env will hardcode it.)"
Write-Host ""
Write-Host "Remaining manual steps on this PC:"
Write-Host "1. Open my.ini (usually C:\ProgramData\MySQL\MySQL Server 8.0\my.ini)."
Write-Host "   Under [mysqld], set:  bind-address = 0.0.0.0"
Write-Host "   (or delete/comment out any existing bind-address line -- it currently restricts MySQL to 127.0.0.1)."
Write-Host "2. Restart the service:  Restart-Service MySQL80"
Write-Host "3. Open a MySQL client (mysql -u root -p) and run, choosing your own password:"
Write-Host "     CREATE USER 'quant_remote'@'$Subnet' IDENTIFIED BY 'choose-a-strong-password';"
Write-Host "     GRANT ALL PRIVILEGES ON quant_app.* TO 'quant_remote'@'$Subnet';"
Write-Host "     FLUSH PRIVILEGES;"
Write-Host "   ('$Subnet' scopes the grant to this PC's LAN, not the whole internet -- adjust if your router uses a different subnet.)"
Write-Host "4. On the laptop's .env, set:"
Write-Host "     MYSQL_HOST=$PcIp"
Write-Host "     MYSQL_USER=quant_remote"
Write-Host "     MYSQL_PASSWORD=<the password you chose above>"
