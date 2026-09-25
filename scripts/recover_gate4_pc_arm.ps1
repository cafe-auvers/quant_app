param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$EvidenceBundle,
    [Parameter(Mandatory = $true)][string]$SessionDate,
    [ValidateRange(1, 30)][int]$TimeoutMinutes = 20
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$bundle = (Resolve-Path -LiteralPath $EvidenceBundle).Path
$python = Join-Path $repo "venv\Scripts\python.exe"
$journal = Join-Path $bundle "gate4.evidence.jsonl"
$config = Join-Path $repo "config\runtime.local.json"
$log = Join-Path $bundle ("gate4_" + $SessionDate + "_recovery_arm.log")
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Gate4Log([string]$Message) {
    Add-Content -LiteralPath $log `
        -Value ((Get-Date).ToString("o") + " " + $Message) -Encoding UTF8
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

function Get-SessionEvents {
    @(
        Get-Content -LiteralPath $journal | ForEach-Object {
            try { $_ | ConvertFrom-Json } catch { $null }
        } | Where-Object { $_.payload.session_date -eq $SessionDate }
    )
}

function Invoke-ExecutionOwnerPcUi {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    if (-not ("Gate4RecoveryMouse" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class Gate4RecoveryMouse {
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
    [void][Gate4RecoveryMouse]::SetForegroundWindow(
        [IntPtr]$window.Current.NativeWindowHandle
    )
    Start-Sleep -Milliseconds 500

    $buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    )
    $pcButtons = @(
        $window.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $buttonCondition
        ) | Where-Object {
            $_.Current.Name -eq "PC" -and
            $_.Current.IsEnabled -and
            -not $_.Current.IsOffscreen
        } | Sort-Object {
            $_.Current.BoundingRectangle.X
        }
    )
    if ($pcButtons.Count -eq 0) {
        throw "Enabled Execution Owner PC button was not found"
    }
    $button = $pcButtons[0]
    try {
        $pattern = $button.GetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern
        )
        $pattern.Invoke()
    } catch {
        try {
            $legacy = $button.GetCurrentPattern(
                [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern
            )
            $legacy.DoDefaultAction()
        } catch {
            $bounds = $button.Current.BoundingRectangle
            if ($bounds.IsEmpty) {
                throw "Execution Owner PC button is not invokable"
            }
            [void][Gate4RecoveryMouse]::SetCursorPos(
                [int]($bounds.X + ($bounds.Width / 2)),
                [int]($bounds.Y + ($bounds.Height / 2))
            )
            [Gate4RecoveryMouse]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
            Start-Sleep -Milliseconds 120
            [Gate4RecoveryMouse]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
        }
    }
}

function Invoke-ManualArmUi {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    if (-not ("Gate4RecoveryMouse" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class Gate4RecoveryMouse {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
}
"@
    }

    function Invoke-AutomationElement(
        [System.Windows.Automation.AutomationElement]$Element,
        [string]$Description
    ) {
        try {
            $pattern = $Element.GetCurrentPattern(
                [System.Windows.Automation.InvokePattern]::Pattern
            )
            $pattern.Invoke()
            return
        } catch {
            try {
                $legacy = $Element.GetCurrentPattern(
                    [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern
                )
                $legacy.DoDefaultAction()
                return
            } catch {
                $bounds = $Element.Current.BoundingRectangle
                if ($bounds.IsEmpty) {
                    throw "$Description has no invokable pattern or clickable bounds"
                }
                [void][Gate4RecoveryMouse]::SetCursorPos(
                    [int]($bounds.X + ($bounds.Width / 2)),
                    [int]($bounds.Y + ($bounds.Height / 2))
                )
                [Gate4RecoveryMouse]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
                Start-Sleep -Milliseconds 120
                [Gate4RecoveryMouse]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
            }
        }
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
    [void][Gate4RecoveryMouse]::SetForegroundWindow(
        [IntPtr]$window.Current.NativeWindowHandle
    )
    Start-Sleep -Milliseconds 500

    $buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    )
    $armButton = @(
        $window.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition
        ) | Where-Object {
            $_.Current.Name -like "LIVE TRADING*" -and $_.Current.IsEnabled
        }
    ) | Select-Object -First 1
    if ($armButton) {
        Invoke-AutomationElement $armButton "Live Trading button"
    } else {
        # Qt does not expose a checkable QPushButton hosted in QMenuBar's
        # corner widget through UI Automation on this PC. The dashboard uses
        # a fixed maximized layout; derive the reviewed button center from the
        # window bounds instead of an absolute screen coordinate.
        $bounds = $window.Current.BoundingRectangle
        if ($bounds.IsEmpty -or $bounds.Width -lt 1200 -or $bounds.Height -lt 700) {
            throw "Live Trading control was not accessible and dashboard bounds are unsafe"
        }
        [void][Gate4RecoveryMouse]::SetCursorPos(
            [int]($bounds.X + ($bounds.Width * 0.515)),
            [int]($bounds.Y + 12)
        )
        [Gate4RecoveryMouse]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 120
        [Gate4RecoveryMouse]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    }
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
    [void][Gate4RecoveryMouse]::SetForegroundWindow(
        [IntPtr]$dialog.Current.NativeWindowHandle
    )
    $yesButton = @(
        $dialog.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $buttonCondition
        ) | Where-Object {
            $_.Current.IsEnabled -and
            ($_.Current.Name -match "^(?:&?Yes|Y&es)$")
        }
    ) | Select-Object -First 1
    if (-not $yesButton) { throw "Enabled Yes button was not found" }
    Invoke-AutomationElement $yesButton "Live Trading confirmation button"
}

try {
    Set-Location -LiteralPath $repo
    Write-Gate4Log "Gate 4 recovery arm waiting for runtime ACTIVE"
    Set-Gate4Collection $true

    $events = Get-SessionEvents
    if (@($events | Where-Object { $_.event_type -eq "SESSION_ENDED" }).Count -gt 0) {
        throw "The Gate 4 session is already closed"
    }
    if (@($events | Where-Object { $_.event_type -eq "SESSION_STARTED" }).Count -ne 1) {
        throw "Recovery requires exactly one open supervised session"
    }
    if (@($events | Where-Object { $_.event_type -eq "MANUAL_ARM" }).Count -gt 0) {
        Write-Gate4Log "Gate 4 session was already armed"
        exit 0
    }

    $active = @($events | Where-Object { $_.event_type -eq "RUNTIME_ACTIVE" })
    if ($active.Count -eq 0) {
        Invoke-ExecutionOwnerPcUi
        Write-Gate4Log "Requested the normal Execution Owner PC handoff through the dashboard"
    } else {
        Write-Gate4Log "Runtime was already ACTIVE; skipped the redundant owner handoff"
    }
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    do {
        Start-Sleep -Seconds 2
        $events = Get-SessionEvents
        $active = @($events | Where-Object { $_.event_type -eq "RUNTIME_ACTIVE" })
    } while ($active.Count -eq 0 -and (Get-Date) -lt $deadline)
    if ($active.Count -eq 0) {
        throw "Runtime did not reach ACTIVE before the recovery timeout"
    }

    & $python (Join-Path $bundle "release_execution_owner_if_flat.py") `
        --repository $repo --verify-only *>> $log
    if ($LASTEXITCODE -ne 0) {
        throw "Flat-state proof failed before recovery arm"
    }

    Invoke-ManualArmUi
    $armDeadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Seconds 1
        $events = Get-SessionEvents
        $arms = @($events | Where-Object { $_.event_type -eq "MANUAL_ARM" })
    } while ($arms.Count -eq 0 -and (Get-Date) -lt $armDeadline)
    if ($arms.Count -eq 0) { throw "Manual UI arm was not recorded" }
    Write-Gate4Log "Gate 4 recovery arm completed after runtime ACTIVE"
} catch {
    Write-Gate4Log ("BLOCKED: " + $_.Exception.Message)
    Set-Gate4Collection $false
    throw
}
