[CmdletBinding()]
param(
    [string]$SourceEnv,
    [string]$DestinationEnv,
    [string]$TemplateEnv
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

$sourcePath = [System.IO.Path]::GetFullPath($SourceEnv)
$destinationPath = [System.IO.Path]::GetFullPath($DestinationEnv)
$templatePath = [System.IO.Path]::GetFullPath($TemplateEnv)

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
& $pythonExe $syncScript --template $templatePath --env $sourcePath --pc-env $destinationPath
if ($LASTEXITCODE -ne 0) {
    throw "Environment synchronization exited with code $LASTEXITCODE."
}
