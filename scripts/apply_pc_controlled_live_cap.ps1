<#
Apply an explicitly approved controlled-live entry cap to the qualified PC.

The operation waits for a sleeping PC to become reachable, proves that live
control is OFF and broker/order state is flat, synchronizes legacy runtime
settings out of .env, updates the gitignored runtime override, and runs the
controlled-live preflight from a fresh process.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(0.01, 1000000000)]
    [decimal]$EntryNotional,

    [Parameter(Mandatory = $true)]
    [string]$PcTailscaleIp,

    [string]$RepositoryRoot = "C:\Users\tonyh\quant_app",
    [string]$EvidenceBundle = "C:\Users\tonyh\quant_evidence\gate4_dca259c_20260924",

    [ValidateRange(1, 60)]
    [int]$RetryMinutes = 15,

    [string]$ApprovedBy = "tonyh-owner-operator",
    [string]$ApprovedSymbol = "",
    [string]$LogPath = (Join-Path $env:LOCALAPPDATA "quant_app\gate4_cap_deployment.log")
)

$ErrorActionPreference = "Stop"
$invokePc = Join-Path $PSScriptRoot "invoke_pc_command.ps1"
$logDirectory = Split-Path -Parent $LogPath
if ($logDirectory) {
    [void](New-Item -ItemType Directory -Path $logDirectory -Force)
}
$capText = $EntryNotional.ToString(
    "0.00",
    [Globalization.CultureInfo]::InvariantCulture
)
$approvedSymbolText = ([string]$ApprovedSymbol).Trim().ToUpperInvariant()
if ($approvedSymbolText -and $approvedSymbolText -notmatch '^[A-Z0-9.-]{1,20}$') {
    throw "ApprovedSymbol is invalid"
}
$deadline = (Get-Date).AddMinutes($RetryMinutes)

function Write-DeploymentLog([string]$Message) {
    Add-Content -LiteralPath $LogPath `
        -Value ((Get-Date).ToString("o") + " " + $Message) -Encoding UTF8
}

Write-DeploymentLog (
    "Waiting to apply approved controlled-live entry cap USD " + $capText
)

while ($true) {
    try {
        $output = & $invokePc -PcTailscaleIp $PcTailscaleIp `
            -ArgumentList @(
                $RepositoryRoot,
                $EvidenceBundle,
                $capText,
                $ApprovedBy,
                $approvedSymbolText
            ) -ScriptBlock {
                param($repo, $bundle, $approvedCap, $approvedBy, $approvedSymbol)

                $ErrorActionPreference = "Stop"
                $python = Join-Path $repo "venv\Scripts\python.exe"
                $runtimePath = Join-Path $repo "config\runtime.local.json"
                $syncScript = Join-Path $repo "scripts\sync_pc_env.ps1"
                $disarmScript = Join-Path $bundle "disarm_gate4_session.py"
                $flatProofScript = Join-Path $bundle "release_execution_owner_if_flat.py"
                $preflightPath = Join-Path $bundle "controlled_live_preflight_approved_cap.json"
                $approvalPath = Join-Path $bundle "controlled_live_entry_cap_approval.json"
                $utf8 = New-Object System.Text.UTF8Encoding($false)

                foreach ($requiredPath in @(
                    $python,
                    $runtimePath,
                    $syncScript,
                    $disarmScript,
                    $flatProofScript
                )) {
                    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
                        throw "Required deployment file is missing: $requiredPath"
                    }
                }

                Set-Location -LiteralPath $repo
                $runtime = Get-Content -Raw -LiteralPath $runtimePath | ConvertFrom-Json
                $head = (& git rev-parse HEAD).Trim().ToLowerInvariant()
                $configuredCommit = ([string]$runtime.KIS_RUNTIME_COMMIT_SHA).Trim().ToLowerInvariant()
                if ($head -ne $configuredCommit) {
                    throw "Repository HEAD does not match the qualified runtime commit"
                }
                if ((& git status --porcelain)) {
                    throw "Qualified runtime checkout is not clean"
                }

                & $python $disarmScript --repository $repo --no-evidence
                if ($LASTEXITCODE -ne 0) {
                    throw "Could not establish shared live-control OFF state"
                }
                & $python $flatProofScript --repository $repo --verify-only
                if ($LASTEXITCODE -ne 0) {
                    throw "Broker/order flat-state proof failed"
                }

                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $syncScript
                if ($LASTEXITCODE -ne 0) {
                    throw "Credential/runtime synchronization failed before cap update"
                }

                $runtime = Get-Content -Raw -LiteralPath $runtimePath | ConvertFrom-Json
                $key = "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL"
                if ($null -eq $runtime.PSObject.Properties[$key]) {
                    $runtime | Add-Member -NotePropertyName $key -NotePropertyValue $approvedCap
                } else {
                    $runtime.$key = $approvedCap
                }
                [IO.File]::WriteAllText(
                    $runtimePath,
                    (($runtime | ConvertTo-Json -Depth 20) + "`n"),
                    $utf8
                )

                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $syncScript
                if ($LASTEXITCODE -ne 0) {
                    throw "Credential/runtime synchronization failed after cap update"
                }

                $effectiveText = & $python -c (
                    "from src.utils.config import install_repository_configuration; " +
                    "install_repository_configuration(); " +
                    "from src.core.execution_config import " +
                    "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL as value; print(value)"
                )
                if ($LASTEXITCODE -ne 0) {
                    throw "Could not read the effective controlled-live cap"
                }
                $effective = [decimal]::Parse(
                    ([string]$effectiveText).Trim(),
                    [Globalization.CultureInfo]::InvariantCulture
                )
                $approved = [decimal]::Parse(
                    $approvedCap,
                    [Globalization.CultureInfo]::InvariantCulture
                )
                if ($effective -ne $approved) {
                    throw "Effective controlled-live cap does not match the approval"
                }

                & $python (Join-Path $repo "scripts\check_controlled_live_readiness.py") `
                    --json-output $preflightPath
                if ($LASTEXITCODE -ne 0) {
                    throw "Controlled-live preflight failed after the cap update"
                }

                $approval = [ordered]@{
                    schema_version = 1
                    approved_at = [DateTimeOffset]::Now.ToString("o")
                    approved_by = $approvedBy
                    approved_entry_notional_usd = [double]$approved
                    approved_symbols = @(
                        if ($approvedSymbol) { $approvedSymbol }
                    )
                    runtime_commit_sha = $head
                    preflight_path = $preflightPath
                    control_off_during_change = $true
                    flat_state_verified = $true
                }
                [IO.File]::WriteAllText(
                    $approvalPath,
                    (($approval | ConvertTo-Json -Depth 10) + "`n"),
                    $utf8
                )

                [pscustomobject]@{
                    approved_entry_notional_usd = [double]$approved
                    approved_symbol = $approvedSymbol
                    effective_entry_notional_usd = [double]$effective
                    runtime_commit_sha = $head
                    control_off = $true
                    flat_state_verified = $true
                    preflight = "PASSED"
                    approval_record = $approvalPath
                } | ConvertTo-Json -Compress
            }

        foreach ($line in @($output)) {
            Write-DeploymentLog ([string]$line)
        }
        Write-DeploymentLog "Controlled-live entry cap deployment completed"
        $output
        exit 0
    } catch {
        Write-DeploymentLog ("Attempt failed: " + $_.Exception.Message)
        if ((Get-Date) -ge $deadline) {
            Write-DeploymentLog "Deployment deadline expired"
            throw
        }
        Start-Sleep -Seconds 30
    }
}
