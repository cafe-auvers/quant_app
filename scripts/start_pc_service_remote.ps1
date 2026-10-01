<#
.SYNOPSIS
    Start the PC listener or dashboard from the laptop through guarded WinRM.

.DESCRIPTION
    Uses the existing DPAPI-protected WinRM credential and registers an
    on-demand interactive task on the PC. Starting main.py is refused while a
    combined Gate-2/Gate-3 collector is active or scheduled within 12 hours.
    The listener remains safe to start during qualification because it does
    not open a KIS WebSocket.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Listener", "Main")]
    [string]$Service,

    [string]$RemoteRepositoryRoot = "C:\Users\tonyh\quant_app"
)

$ErrorActionPreference = "Stop"
$Invoker = Join-Path $PSScriptRoot "invoke_pc_command.ps1"

function Write-ResultJson {
    param(
        [bool]$Success,
        [string]$Message,
        [bool]$ListenerRunning = $false,
        [bool]$MainRunning = $false,
        [bool]$QualificationGuardActive = $false
    )

    [pscustomobject]@{
        success = $Success
        service = $Service.ToLowerInvariant()
        message = $Message
        listener_running = $ListenerRunning
        main_running = $MainRunning
        qualification_guard_active = $QualificationGuardActive
    } | ConvertTo-Json -Compress
}

try {
    if (-not (Test-Path -LiteralPath $Invoker -PathType Leaf)) {
        throw "PC WinRM helper is missing: $Invoker"
    }

    $RemoteAction = {
        param([string]$RequestedService, [string]$RepositoryRoot)

        $ErrorActionPreference = "Stop"
        $repo = (Resolve-Path -LiteralPath $RepositoryRoot).Path
        $listenerScript = (Resolve-Path -LiteralPath (
            Join-Path $repo "scripts\pc_remote_control_listener.py"
        )).Path
        $mainScript = (Resolve-Path -LiteralPath (Join-Path $repo "main.py")).Path

        function Get-MatchingPythonProcess([string]$ScriptPath) {
            return @(
                Get-CimInstance Win32_Process -ErrorAction Stop |
                    Where-Object {
                        $_.Name -match '^python(w)?\.exe$' -and
                        $_.CommandLine -and
                        $_.CommandLine.IndexOf(
                            $ScriptPath,
                            [System.StringComparison]::OrdinalIgnoreCase
                        ) -ge 0
                    }
            )
        }

        function Get-QualificationGuard {
            $active = @(
                Get-CimInstance Win32_Process -ErrorAction Stop |
                    Where-Object {
                        $_.Name -match '^python(w)?\.exe$' -and
                        $_.CommandLine -and
                        $_.CommandLine -match (
                            'manage_gate2_session|run_gate2_soak|' +
                            'run_gate3_shadow|capture_kis_ws'
                        )
                    }
            )
            if ($active.Count -gt 0) {
                return [pscustomobject]@{
                    Active = $true
                    Reason = "a Gate 2/3 qualification process is running"
                }
            }

            $now = Get-Date
            $deadline = $now.AddHours(12)
            foreach ($task in @(
                Get-ScheduledTask -ErrorAction SilentlyContinue |
                    Where-Object {
                        $_.TaskName -match '^QuantApp_Gate23_Combined_\d{8}$' -and
                        $_.Settings.Enabled
                    }
            )) {
                if ([string]$task.State -eq "Running") {
                    return [pscustomobject]@{
                        Active = $true
                        Reason = "the combined Gate 2/3 scheduled task is running"
                    }
                }
                $info = $task | Get-ScheduledTaskInfo
                $next = $info.NextRunTime
                if ($null -ne $next -and $next -gt $now -and $next -le $deadline) {
                    return [pscustomobject]@{
                        Active = $true
                        Reason = (
                            "combined Gate 2/3 is scheduled at " +
                            $next.ToString("yyyy-MM-dd HH:mm:ss")
                        )
                    }
                }
            }
            return [pscustomobject]@{ Active = $false; Reason = "" }
        }

        $listenerBefore = @(Get-MatchingPythonProcess $listenerScript).Count -gt 0
        $mainBefore = @(Get-MatchingPythonProcess $mainScript).Count -gt 0

        if ($RequestedService -eq "Main") {
            $guard = Get-QualificationGuard
            if ($guard.Active) {
                return [pscustomobject]@{
                    success = $false
                    service = "main"
                    message = (
                        "PC main.py start refused because $($guard.Reason). " +
                        "Keep the qualification WebSocket isolated."
                    )
                    listener_running = $listenerBefore
                    main_running = $mainBefore
                    qualification_guard_active = $true
                }
            }
            if ($mainBefore) {
                return [pscustomobject]@{
                    success = $true
                    service = "main"
                    message = "PC main.py is already running."
                    listener_running = $listenerBefore
                    main_running = $true
                    qualification_guard_active = $false
                }
            }
        } elseif ($listenerBefore) {
            return [pscustomobject]@{
                success = $true
                service = "listener"
                message = "The PC listener is already running."
                listener_running = $true
                main_running = $mainBefore
                qualification_guard_active = $false
            }
        }

        $venvScripts = Join-Path $repo "venv\Scripts"
        if ($RequestedService -eq "Listener") {
            # This workstation has an explicit inbound firewall block for its
            # Anaconda pythonw.exe. python.exe is allowed by the listener's
            # narrowly scoped Tailscale-port firewall rule.
            $python = Join-Path $venvScripts "python.exe"
            $script = $listenerScript
            $taskName = "QuantApp_StartListenerOnDemand"
            $description = "Starts the restricted QuantApp PC listener on demand."
        } else {
            $python = Join-Path $venvScripts "pythonw.exe"
            if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
                $python = Join-Path $venvScripts "python.exe"
            }
            $script = $mainScript
            $taskName = "QuantApp_StartMainOnDemand"
            $description = "Starts the QuantApp PC dashboard on demand."
        }
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
            throw "The PC virtual-environment Python is missing: $python"
        }

        $interactiveUser = (Get-CimInstance Win32_ComputerSystem).UserName
        if (-not $interactiveUser) {
            throw "No logged-in PC desktop user is available for the dashboard task."
        }
        $action = New-ScheduledTaskAction -Execute $python `
            -Argument "`"$script`"" -WorkingDirectory $repo
        $principal = New-ScheduledTaskPrincipal -UserId $interactiveUser `
            -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $taskName -Action $action `
            -Principal $principal -Settings $settings `
            -Description $description -Force | Out-Null
        Start-ScheduledTask -TaskName $taskName

        $target = if ($RequestedService -eq "Listener") {
            $listenerScript
        } else {
            $mainScript
        }
        $deadline = (Get-Date).AddSeconds(15)
        do {
            Start-Sleep -Milliseconds 500
            $started = @(Get-MatchingPythonProcess $target).Count -gt 0
        } while (-not $started -and (Get-Date) -lt $deadline)

        $listenerAfter = @(Get-MatchingPythonProcess $listenerScript).Count -gt 0
        $mainAfter = @(Get-MatchingPythonProcess $mainScript).Count -gt 0
        if (-not $started) {
            return [pscustomobject]@{
                success = $false
                service = $RequestedService.ToLowerInvariant()
                message = "The PC task ran, but the requested process did not remain active."
                listener_running = $listenerAfter
                main_running = $mainAfter
                qualification_guard_active = $false
            }
        }
        $label = if ($RequestedService -eq "Listener") { "listener" } else { "main.py" }
        return [pscustomobject]@{
            success = $true
            service = $RequestedService.ToLowerInvariant()
            message = "PC $label started successfully."
            listener_running = $listenerAfter
            main_running = $mainAfter
            qualification_guard_active = $false
        }
    }

    $response = @(
        & $Invoker -ScriptBlock $RemoteAction `
            -ArgumentList @($Service, $RemoteRepositoryRoot)
    ) | Select-Object -Last 1
    if ($null -eq $response) {
        throw "The PC did not return a service-start result."
    }
    [pscustomobject]@{
        success = [bool]$response.success
        service = [string]$response.service
        message = [string]$response.message
        listener_running = [bool]$response.listener_running
        main_running = [bool]$response.main_running
        qualification_guard_active = [bool]$response.qualification_guard_active
    } | ConvertTo-Json -Compress
} catch {
    Write-ResultJson -Success $false -Message $_.Exception.Message
    exit 1
}
