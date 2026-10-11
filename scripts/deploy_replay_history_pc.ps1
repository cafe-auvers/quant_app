param([Parameter(Mandatory=$true)][string]$BundlePath)
$ErrorActionPreference = 'Stop'
$TaskStudyBundle = [IO.Path]::GetFullPath($BundlePath)
if (-not (Test-Path -LiteralPath $TaskStudyBundle)) { throw 'The reviewed study maintenance bundle is required.' }
$TaskStudyPayload = [Convert]::ToBase64String([IO.File]::ReadAllBytes($TaskStudyBundle))
$TaskStudyHash = (Get-FileHash -LiteralPath $TaskStudyBundle -Algorithm SHA256).Hash
& (Join-Path $PSScriptRoot 'invoke_pc_command.ps1') -ArgumentList @($TaskStudyPayload, $TaskStudyHash) -ScriptBlock {
    param([string]$BundleBase64, [string]$ExpectedHash)
    $ErrorActionPreference = 'Stop'
    $TaskStudyPcRoot = 'C:\Users\tonyh\quant_app'
    $TaskStudyDeploymentRoot = Join-Path $env:LOCALAPPDATA 'quant_app\replay_maintenance'
    $TaskStudyExisting = Get-ScheduledTask -TaskName 'QuantApp_ReplayDailyHistory' -ErrorAction SilentlyContinue
    if ($TaskStudyExisting) { Stop-ScheduledTask -TaskName 'QuantApp_ReplayDailyHistory' }
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
        $_.CommandLine -like ('*' + $TaskStudyDeploymentRoot + '*prepare_replay.py*--role history*')
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Path $TaskStudyDeploymentRoot -Force | Out-Null
    $TaskStudyZip = Join-Path $TaskStudyDeploymentRoot 'maintenance.zip'
    [IO.File]::WriteAllBytes($TaskStudyZip, [Convert]::FromBase64String($BundleBase64))
    if ((Get-FileHash -LiteralPath $TaskStudyZip -Algorithm SHA256).Hash -ne $ExpectedHash) { throw 'Maintenance bundle verification failed.' }
    & (Join-Path $TaskStudyPcRoot 'venv\Scripts\python.exe') -m zipfile -e $TaskStudyZip $TaskStudyDeploymentRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not extract the maintenance bundle.' }
    Copy-Item -LiteralPath (Join-Path $TaskStudyPcRoot '.env') -Destination (Join-Path $TaskStudyDeploymentRoot '.env') -Force
    $TaskStudyDefaultsPath = Join-Path $TaskStudyDeploymentRoot 'config\runtime.json'
    $TaskStudyDefaults = Get-Content -LiteralPath $TaskStudyDefaultsPath -Raw | ConvertFrom-Json
    $TaskStudyPcDefaults = Get-Content -LiteralPath (Join-Path $TaskStudyPcRoot 'config\runtime.json') -Raw | ConvertFrom-Json
    foreach ($TaskStudyProperty in $TaskStudyPcDefaults.PSObject.Properties) {
        if (-not $TaskStudyDefaults.PSObject.Properties[$TaskStudyProperty.Name]) {
            $TaskStudyDefaults | Add-Member -NotePropertyName $TaskStudyProperty.Name -NotePropertyValue $TaskStudyProperty.Value
        }
    }
    $TaskStudyDefaults | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $TaskStudyDefaultsPath -Encoding UTF8
    $TaskStudyPcLocal = Join-Path $TaskStudyPcRoot 'config\runtime.local.json'
    if (Test-Path -LiteralPath $TaskStudyPcLocal) {
        Copy-Item -LiteralPath $TaskStudyPcLocal -Destination (Join-Path $TaskStudyDeploymentRoot 'config\runtime.local.json') -Force
    }
    & (Join-Path $TaskStudyDeploymentRoot 'scripts\start_replay_preparation.ps1') -Role history -PythonPath (Join-Path $TaskStudyPcRoot 'venv\Scripts\python.exe') -DailyRefreshRoot $TaskStudyPcRoot -InstallStartupTask -RegisterOnly
    Start-ScheduledTask -TaskName 'QuantApp_ReplayDailyHistory'
    [pscustomobject]@{ Role='history'; BundleVerified=$true; DeploymentRoot=$TaskStudyDeploymentRoot; TaskState=(Get-ScheduledTask -TaskName 'QuantApp_ReplayDailyHistory').State; ExistingDailyPipeline='uses its daily lock and batch downloader'; LiveAppSourceModified=$false } | ConvertTo-Json -Compress
}
