param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$EvidenceBundle,
    [Parameter(Mandatory = $true)][string]$SessionDate,
    [string]$Supervisor = "tonyh-owner-operator"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$bundle = (Resolve-Path -LiteralPath $EvidenceBundle).Path
$python = Join-Path $repo "venv\Scripts\python.exe"
$config = Join-Path $repo "config\runtime.local.json"
$journal = Join-Path $bundle "gate4.evidence.jsonl"
$log = Join-Path $bundle ("gate4_" + $SessionDate + "_start.log")
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Gate4Log([string]$Message) {
    Add-Content -LiteralPath $log -Value ((Get-Date).ToString("o") + " " + $Message) -Encoding UTF8
}

function Set-Gate4Collection([bool]$Enabled) {
    $runtime = Get-Content -Raw -LiteralPath $config | ConvertFrom-Json
    $runtime.GATE4_QUALIFICATION_ENABLED = $(if ($Enabled) { "true" } else { "false" })
    $runtime.GATE4_EVIDENCE_JOURNAL_PATH = $journal
    [IO.File]::WriteAllText(
        $config,
        (($runtime | ConvertTo-Json -Depth 20) + "`n"),
        $utf8
    )
}

function Get-DashboardProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -match "quant_app\\main\.py"
    })
}

function Request-DashboardClose {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    if (-not ("Gate4WindowClose" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class Gate4WindowClose {
    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
}
"@
    }
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        "Stock Dashboard"
    )
    $window = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $condition
    )
    if ($window) {
        [void][Gate4WindowClose]::PostMessage(
            [IntPtr]$window.Current.NativeWindowHandle,
            16,
            [IntPtr]::Zero,
            [IntPtr]::Zero
        )
    }
}

function Stop-ExistingDashboard {
    if ((Get-DashboardProcesses).Count -eq 0) { return }
    Request-DashboardClose
    $deadline = (Get-Date).AddSeconds(35)
    do {
        Start-Sleep -Milliseconds 500
    } while ((Get-DashboardProcesses).Count -gt 0 -and (Get-Date) -lt $deadline)
    if ((Get-DashboardProcesses).Count -eq 0) { return }

    & $python (Join-Path $bundle "release_execution_owner_if_flat.py") `
        --repository $repo *>> $log
    if ($LASTEXITCODE -ne 0) {
        throw "Existing dashboard did not close and flat-state lease release failed"
    }
    foreach ($process in (Get-DashboardProcesses)) {
        Stop-Process -Id $process.ProcessId -Force
    }
    Start-Sleep -Seconds 2
    if ((Get-DashboardProcesses).Count -gt 0) {
        throw "Could not stop the existing dashboard"
    }
}

function Get-SessionEvents {
    @(
        Get-Content -LiteralPath $journal | ForEach-Object {
            try { $_ | ConvertFrom-Json } catch { $null }
        } | Where-Object { $_.payload.session_date -eq $SessionDate }
    )
}

function Invoke-ManualArmUi {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    if (-not ("Gate4Mouse" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class Gate4Mouse {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
}
"@
    }
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $windowCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        "Stock Dashboard"
    )
    $window = $root.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $windowCondition
    )
    if (-not $window) { throw "Stock Dashboard window was not found" }
    $bounds = $window.Current.BoundingRectangle
    [void][Gate4Mouse]::SetForegroundWindow([IntPtr]$window.Current.NativeWindowHandle)
    Start-Sleep -Milliseconds 500
    [void][Gate4Mouse]::SetCursorPos(
        [int]($bounds.X + ($bounds.Width / 2)),
        [int]($bounds.Y + 11)
    )
    [Gate4Mouse]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 120
    [Gate4Mouse]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Seconds 2

    $dialogCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        "Enable Live Trading"
    )
    $dialog = $root.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $dialogCondition
    )
    if (-not $dialog) { throw "Live-trading confirmation dialog did not open" }
    $dialogBounds = $dialog.Current.BoundingRectangle
    [void][Gate4Mouse]::SetForegroundWindow([IntPtr]$dialog.Current.NativeWindowHandle)
    [void][Gate4Mouse]::SetCursorPos(
        [int]($dialogBounds.X + $dialogBounds.Width - 103),
        [int]($dialogBounds.Y + $dialogBounds.Height - 28)
    )
    [Gate4Mouse]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 120
    [Gate4Mouse]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
}

try {
    Set-Location -LiteralPath $repo
    Write-Gate4Log "Gate 4 supervised start beginning"
    Set-Gate4Collection $false
    & $python (Join-Path $bundle "disarm_gate4_session.py") `
        --repository $repo --session-date $SessionDate --no-evidence *>> $log
    if ($LASTEXITCODE -ne 0) { throw "Could not establish shared OFF state" }
    Stop-ExistingDashboard

    $head = (& git rev-parse HEAD).Trim().ToLowerInvariant()
    $configuredCommit = [string](
        (Get-Content -Raw -LiteralPath $config | ConvertFrom-Json).KIS_RUNTIME_COMMIT_SHA
    )
    if ($head -ne $configuredCommit.Trim().ToLowerInvariant()) {
        throw "Repository HEAD does not match KIS_RUNTIME_COMMIT_SHA"
    }
    if ((& git status --porcelain)) { throw "Gate 4 requires a clean checkout" }

    Set-Gate4Collection $true
    $preflight = Join-Path $bundle ("controlled_live_preflight_" + $SessionDate + ".json")
    & $python ".\scripts\check_controlled_live_readiness.py" `
        --json-output $preflight *>> $log
    if ($LASTEXITCODE -ne 0) { throw "Controlled-live preflight failed" }

    $latest = Get-Content -Raw "C:\Users\tonyh\quant_evidence\gate2_sessions\latest_session.json" | ConvertFrom-Json
    $gate3 = Join-Path ([string]$latest.session_dir) "gate3\gate3_report.json"
    & $python ".\scripts\manage_gate4_session.py" start-session `
        --gate3-report $gate3 --journal $journal --session-date $SessionDate `
        --supervisor $Supervisor *>> $log
    if ($LASTEXITCODE -ne 0) { throw "Gate 4 start-session failed" }

    $stdout = Join-Path $bundle ("main_py_gate4_" + $SessionDate + "_stdout.log")
    $stderr = Join-Path $bundle ("main_py_gate4_" + $SessionDate + "_stderr.log")
    Start-Process -FilePath $python -ArgumentList (Join-Path $repo "main.py") `
        -WorkingDirectory $repo -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr

    $activeDeadline = (Get-Date).AddMinutes(4)
    do {
        Start-Sleep -Seconds 2
        $active = @(Get-SessionEvents | Where-Object { $_.event_type -eq "RUNTIME_ACTIVE" })
    } while ($active.Count -eq 0 -and (Get-Date) -lt $activeDeadline)
    if ($active.Count -eq 0) { throw "Runtime did not reach ACTIVE before timeout" }

    Invoke-ManualArmUi
    $armDeadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Seconds 1
        $arms = @(Get-SessionEvents | Where-Object { $_.event_type -eq "MANUAL_ARM" })
    } while ($arms.Count -eq 0 -and (Get-Date) -lt $armDeadline)
    if ($arms.Count -eq 0) { throw "Manual UI arm was not recorded" }
    Write-Gate4Log "Gate 4 runtime ACTIVE and manually armed through the dashboard"
} catch {
    Write-Gate4Log ("BLOCKED: " + $_.Exception.Message)
    Set-Gate4Collection $false
    throw
}
