[CmdletBinding()]
param(
    [string]$SourceEnv,
    [string]$DestinationEnv
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

$sourcePath = [System.IO.Path]::GetFullPath($SourceEnv)
$destinationPath = [System.IO.Path]::GetFullPath($DestinationEnv)

if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "Source environment file does not exist: $sourcePath"
}
if ([string]::Equals($sourcePath, $destinationPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "SourceEnv and DestinationEnv must be different files."
}

$assignmentPattern = '^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*)=(.*)$'
$redactedCount = 0

$body = foreach ($line in [System.IO.File]::ReadAllLines($sourcePath)) {
    $match = [System.Text.RegularExpressions.Regex]::Match($line, $assignmentPattern)
    if (-not $match.Success) {
        $line
        continue
    }

    $key = $match.Groups[2].Value
    if ($key.StartsWith("MYSQL_", [System.StringComparison]::OrdinalIgnoreCase)) {
        $redactedCount += 1
        "{0}{1}{2}=" -f $match.Groups[1].Value, $key, $match.Groups[3].Value
    }
    else {
        $line
    }
}

$header = @(
    "# Generated from .env by scripts/sync_pc_env.ps1.",
    "# All non-MySQL values match .env; fill the blank MYSQL_* values manually on the PC.",
    "# Re-run the script after every .env change; this ignored file will be overwritten.",
    ""
)

$destinationDirectory = Split-Path -Parent $destinationPath
if (-not (Test-Path -LiteralPath $destinationDirectory -PathType Container)) {
    [void](New-Item -ItemType Directory -Path $destinationDirectory -Force)
}

$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllLines($destinationPath, @($header + $body), $utf8WithoutBom)

Write-Output "Created PC environment file: $destinationPath"
Write-Output "Blanked $redactedCount MYSQL_* value(s)."
