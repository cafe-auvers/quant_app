[CmdletBinding()]
param(
    [string]$SourceEnv,
    [string]$DestinationEnv,
    [string]$TemplateEnv,
    [string]$RuntimeDefaults,
    [string]$RuntimeLocal
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($SourceEnv)) {
    $SourceEnv = Join-Path $repoRoot ".env"
}
if ([string]::IsNullOrWhiteSpace($DestinationEnv)) {
    $DestinationEnv = Join-Path $repoRoot ".env.pc"
}
if ([string]::IsNullOrWhiteSpace($TemplateEnv)) {
    $TemplateEnv = Join-Path $repoRoot ".env.example"
}
if ([string]::IsNullOrWhiteSpace($RuntimeDefaults)) {
    $RuntimeDefaults = Join-Path $repoRoot "config\runtime.json"
}
if ([string]::IsNullOrWhiteSpace($RuntimeLocal)) {
    $RuntimeLocal = Join-Path $repoRoot "config\runtime.local.json"
}

$sourcePath = [System.IO.Path]::GetFullPath($SourceEnv)
$destinationPath = [System.IO.Path]::GetFullPath($DestinationEnv)
$templatePath = [System.IO.Path]::GetFullPath($TemplateEnv)
$runtimeDefaultsPath = [System.IO.Path]::GetFullPath($RuntimeDefaults)
$runtimeLocalPath = [System.IO.Path]::GetFullPath($RuntimeLocal)

if ([string]::Equals($sourcePath, $destinationPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "SourceEnv and DestinationEnv must be different files."
}
if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {
    throw "Environment template does not exist: $templatePath"
}

$venvPython = Join-Path $repoRoot "venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $pythonExe = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "No repository venv or python.exe on PATH; cannot synchronize environment files."
    }
    $pythonExe = $pythonCommand.Source
}

$syncScript = Join-Path $repoRoot "scripts\sync_env_files.py"
& $pythonExe $syncScript --template $templatePath --env $sourcePath --pc-env $destinationPath `
    --runtime-defaults $runtimeDefaultsPath --runtime-local $runtimeLocalPath
if ($LASTEXITCODE -ne 0) {
    throw "Environment synchronization exited with code $LASTEXITCODE."
}
