[CmdletBinding()]
param([int]$Port = 8765, [switch]$OpenBrowser)

$ErrorActionPreference = "Stop"
$replayRoot = Split-Path -Parent $PSScriptRoot
$replayUrl = "http://localhost:$Port"
$replayProbeUrl = "http://127.0.0.1:$Port"
$ready = $false
try {
    $page = Invoke-WebRequest -Uri "$replayProbeUrl/" -UseBasicParsing -TimeoutSec 2
    $ready = $page.Content.Contains("Breakout Lab")
} catch {}
if (-not $ready) {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
    $replayLogs = Join-Path $replayRoot "data\logs"
    New-Item -ItemType Directory -Path $replayLogs -Force | Out-Null
    Start-Process -FilePath $pythonPath -ArgumentList @("scripts/run_replay.py", "--port", "$Port") `
        -WorkingDirectory $replayRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $replayLogs "replay_stdout.log") `
        -RedirectStandardError (Join-Path $replayLogs "replay_stderr.log")
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $page = Invoke-WebRequest -Uri "$replayProbeUrl/" -UseBasicParsing -TimeoutSec 2
            if ($page.Content.Contains("Breakout Lab")) { $ready = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
}
if (-not $ready) { throw "Breakout Lab could not start. Check data\logs\replay_stderr.log." }
Write-Output "Breakout Lab: $replayUrl"
if ($OpenBrowser) { Start-Process $replayUrl }
