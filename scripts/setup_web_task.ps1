<#
Register the private web dashboard at Windows logon, with restart on failure.
Use the isolated web environment's PythonPath when it is outside this checkout.
#>
param(
    [string]$PythonPath,
    [string]$ConfigPath,
    [string]$LogPath,
    [string]$TaskName = "QuantApp_WebDashboard",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $PythonPath) {
    $PythonPath = Join-Path $RepoRoot ".venv-web\Scripts\pythonw.exe"
}
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $RepoRoot "config\web.local.json"
}
if (-not $LogPath) {
    $LogPath = Join-Path $RepoRoot "data\logs\web_service.log"
}

# pythonw keeps a background task from opening a console in the user's session.
if ((Split-Path $PythonPath -Leaf) -eq "python.exe") {
    $PythonPath = Join-Path (Split-Path $PythonPath -Parent) "pythonw.exe"
}
if ((Split-Path $PythonPath -Leaf) -ne "pythonw.exe") {
    throw "PythonPath must identify python.exe or pythonw.exe in the web environment."
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$LogPath = [System.IO.Path]::GetFullPath($LogPath)
$Launcher = Join-Path $RepoRoot "scripts\supervise_web.py"
$Arguments = "-u `"$Launcher`" --config `"$ConfigPath`" --log-file `"$LogPath`""
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument $Arguments -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings `
    -Description "Private Quant web dashboard: supervise the server, start at logon, restart after exits, append startup/error logs." `
    -Force | Out-Null
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
}
Write-Host "Registered '$TaskName' for $UserId. Restart delay: 1 minute. Log: $LogPath"
