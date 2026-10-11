param(
    [ValidateSet('prepare', 'history')][string]$Role = 'prepare',
    [string]$PythonPath,
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$InstallStartupTask,
    [switch]$RegisterOnly,
    [string]$DailyRefreshRoot,
    [switch]$WaitForExit
)
$ErrorActionPreference = 'Stop'
$TaskStudyRoot = [IO.Path]::GetFullPath($RepositoryRoot)
if (-not $PythonPath) { $PythonPath = (Get-Command python.exe).Source }
$TaskStudyScript = Join-Path $TaskStudyRoot 'scripts\prepare_replay.py'
if (-not (Test-Path -LiteralPath $PythonPath) -or -not (Test-Path -LiteralPath $TaskStudyScript)) {
    throw 'The Python executable and study preparation script are required.'
}
if ($InstallStartupTask) {
    $TaskStudyName = if ($Role -eq 'history') { 'QuantApp_ReplayDailyHistory' } else { 'QuantApp_ReplayStudyPreparation' }
    $TaskStudyArguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Role ' + $Role + ' -PythonPath "' + $PythonPath + '" -WaitForExit'
    if ($DailyRefreshRoot) { $TaskStudyArguments += ' -DailyRefreshRoot "' + $DailyRefreshRoot + '"' }
    $TaskStudyAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $TaskStudyArguments -WorkingDirectory $TaskStudyRoot
    $TaskStudyTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $TaskStudySettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $TaskStudyPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskStudyName -Action $TaskStudyAction -Trigger $TaskStudyTrigger -Settings $TaskStudySettings -Principal $TaskStudyPrincipal -Force | Out-Null
}
if ($RegisterOnly) {
    if (-not $InstallStartupTask) { throw 'RegisterOnly requires InstallStartupTask.' }
    [pscustomobject]@{ Role = $Role; StartupTaskInstalled = $true; TaskName = $TaskStudyName } | ConvertTo-Json -Compress
    exit 0
}
$TaskStudyRunning = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
    $_.CommandLine -like ('*' + $TaskStudyScript + '*') -and $_.CommandLine -like ('*--role ' + $Role + '*')
})
if ($TaskStudyRunning.Count) {
    [pscustomobject]@{ Role = $Role; AlreadyRunning = $true; ProcessId = $TaskStudyRunning[0].ProcessId } | ConvertTo-Json -Compress
    exit 0
}
$TaskStudyLogRoot = Join-Path $TaskStudyRoot 'data\logs'
New-Item -ItemType Directory -Path $TaskStudyLogRoot -Force | Out-Null
$TaskStudyArguments = '-u "' + $TaskStudyScript + '" --role ' + $Role + ' --watch'
if ($DailyRefreshRoot) { $TaskStudyArguments += ' --daily-refresh-root "' + $DailyRefreshRoot + '"' }
$TaskStudyProcess = Start-Process -FilePath $PythonPath -ArgumentList $TaskStudyArguments -WorkingDirectory $TaskStudyRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $TaskStudyLogRoot ('replay_' + $Role + '.log')) -RedirectStandardError (Join-Path $TaskStudyLogRoot ('replay_' + $Role + '.err.log'))
$TaskStudyProcess.PriorityClass = 'BelowNormal'
[pscustomobject]@{ Role = $Role; ProcessId = $TaskStudyProcess.Id; Log = (Join-Path $TaskStudyLogRoot ('replay_' + $Role + '.log')); StartupTaskInstalled = [bool]$InstallStartupTask } | ConvertTo-Json -Compress
if ($WaitForExit) { $TaskStudyProcess.WaitForExit() }
