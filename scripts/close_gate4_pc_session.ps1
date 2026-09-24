param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$EvidenceBundle,
    [Parameter(Mandatory = $true)][string]$SessionDate
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$bundle = (Resolve-Path -LiteralPath $EvidenceBundle).Path
$python = Join-Path $repo "venv\Scripts\python.exe"
$journal = Join-Path $bundle "gate4.evidence.jsonl"
$config = Join-Path $repo "config\runtime.local.json"
$log = Join-Path $bundle ("gate4_" + $SessionDate + "_close.log")
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Gate4Log([string]$Message) {
    Add-Content -LiteralPath $log -Value ((Get-Date).ToString("o") + " " + $Message) -Encoding UTF8
}

function Get-DashboardProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -match "quant_app\\main\.py"
    })
}

function Get-SessionEvents {
    @(
        Get-Content -LiteralPath $journal | ForEach-Object {
            try { $_ | ConvertFrom-Json } catch { $null }
        } | Where-Object { $_.payload.session_date -eq $SessionDate }
    )
}

function Request-DashboardClose {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class Gate4SessionClose {
    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
}
"@
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        "Stock Dashboard"
    )
    $window = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $condition
    )
    if (-not $window) { throw "Stock Dashboard window was not found" }
    [void][Gate4SessionClose]::PostMessage(
        [IntPtr]$window.Current.NativeWindowHandle,
        16,
        [IntPtr]::Zero,
        [IntPtr]::Zero
    )
}

try {
    Set-Location -LiteralPath $repo
    Write-Gate4Log "Gate 4 controlled close beginning"
    & $python (Join-Path $bundle "disarm_gate4_session.py") `
        --repository $repo --session-date $SessionDate *>> $log
    if ($LASTEXITCODE -ne 0) { throw "Gate 4 disarm/probe failed" }
    & $python (Join-Path $bundle "release_execution_owner_if_flat.py") `
        --repository $repo --verify-only *>> $log
    if ($LASTEXITCODE -ne 0) {
        throw "Controlled close refused because the broker/order state is not flat"
    }

    Request-DashboardClose
    $deadline = (Get-Date).AddSeconds(75)
    do {
        Start-Sleep -Seconds 1
        $events = Get-SessionEvents
        $final = @($events | Where-Object {
            $_.event_type -eq "FINAL_RECONCILIATION" -and
            $_.payload.matches_broker -eq $true
        })
        $ended = @($events | Where-Object {
            $_.event_type -eq "SESSION_ENDED" -and
            $_.payload.clean_shutdown -eq $true
        })
    } while (($final.Count -eq 0 -or $ended.Count -eq 0) -and (Get-Date) -lt $deadline)
    if ($final.Count -eq 0 -or $ended.Count -eq 0) {
        throw "Final reconciliation or clean SESSION_ENDED was not recorded"
    }

    $exitDeadline = (Get-Date).AddSeconds(20)
    while ((Get-DashboardProcesses).Count -gt 0 -and (Get-Date) -lt $exitDeadline) {
        Start-Sleep -Milliseconds 500
    }
    if ((Get-DashboardProcesses).Count -gt 0) {
        & $python (Join-Path $bundle "release_execution_owner_if_flat.py") `
            --repository $repo *>> $log
        if ($LASTEXITCODE -ne 0) {
            throw "Dashboard stayed open and flat-state lease release was refused"
        }
        foreach ($process in (Get-DashboardProcesses)) {
            Stop-Process -Id $process.ProcessId -Force
        }
        Start-Sleep -Seconds 2
    }
    if ((Get-DashboardProcesses).Count -gt 0) {
        throw "Dashboard process remained after the verified controlled close"
    }

    $runtime = Get-Content -Raw -LiteralPath $config | ConvertFrom-Json
    $runtime.GATE4_QUALIFICATION_ENABLED = "false"
    [IO.File]::WriteAllText(
        $config,
        (($runtime | ConvertTo-Json -Depth 20) + "`n"),
        $utf8
    )
    & $python ".\scripts\manage_gate4_session.py" status --journal $journal *>> $log
    if ($LASTEXITCODE -ne 0) { throw "Gate 4 journal audit failed" }
    Write-Gate4Log "Gate 4 controlled close completed"
} catch {
    Write-Gate4Log ("BLOCKED: " + $_.Exception.Message)
    throw
}
