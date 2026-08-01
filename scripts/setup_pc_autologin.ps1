<#
Run this ON THE ALWAYS-ON PC, as Administrator, in the account you want it
to auto-login as. Enables Windows AutoAdminLogon so the PC reaches a real
desktop session (needed for main.py's GUI) after an unattended BIOS/WoL
wake, with no one there to type a login password.

Security trade-off (accepted): AutoAdminLogon stores the account password
in HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon in a form any
local administrator can read. Anyone with physical/admin access to this PC
gets your desktop without a Windows password prompt. Only use this on a
machine you physically trust.

The password is read via Read-Host -AsSecureString directly in this
session on this PC -- it is typed here, not passed in from anywhere else,
and this script does not log, print, or transmit it anywhere.

Usage:
    .\scripts\setup_pc_autologin.ps1
    .\scripts\setup_pc_autologin.ps1 -Username someone   # defaults to $env:USERNAME
#>

param(
    [string]$Username = $env:USERNAME
)

$ErrorActionPreference = "Stop"

$IsElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsElevated) {
    throw "Run this from an elevated (Administrator) PowerShell window -- it writes to HKLM."
}

Write-Host "Configuring auto-login for account: $Username"
$securePassword = Read-Host "Enter the Windows password for '$Username'" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $plainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

$winlogonPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty -Path $winlogonPath -Name "AutoAdminLogon" -Value "1" -Type String
Set-ItemProperty -Path $winlogonPath -Name "DefaultUserName" -Value $Username -Type String
Set-ItemProperty -Path $winlogonPath -Name "DefaultDomainName" -Value $env:COMPUTERNAME -Type String
Set-ItemProperty -Path $winlogonPath -Name "DefaultPassword" -Value $plainPassword -Type String

# Drop the plaintext copy from this session's memory as soon as it's written.
Remove-Variable plainPassword

Write-Host ""
Write-Host "Auto-login configured for '$Username' on $env:COMPUTERNAME."
Write-Host "Test it now with:  Restart-Computer"
Write-Host ""
Write-Host "To undo later:"
Write-Host "  Set-ItemProperty -Path '$winlogonPath' -Name AutoAdminLogon -Value '0'"
Write-Host "  Remove-ItemProperty -Path '$winlogonPath' -Name DefaultPassword -ErrorAction SilentlyContinue"
