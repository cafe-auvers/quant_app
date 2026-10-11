param([switch]$ReadOnlyCheck, [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference='Stop'
$TaskPcRoot=[IO.Path]::GetFullPath($RepositoryRoot)
$TaskRuntimeDefaults=Get-Content -LiteralPath (Join-Path $TaskPcRoot 'config\runtime.json') -Raw | ConvertFrom-Json
$TaskRuntimeLocal=Get-Content -LiteralPath (Join-Path $TaskPcRoot 'config\runtime.local.json') -Raw | ConvertFrom-Json
$TaskApprovedCommit=[string]$TaskRuntimeLocal.KIS_RUNTIME_COMMIT_SHA
if(-not $TaskApprovedCommit) {$TaskApprovedCommit=[string]$TaskRuntimeDefaults.KIS_RUNTIME_COMMIT_SHA}
if($TaskApprovedCommit -notmatch '^[a-f0-9]{40}$') {throw 'No immutable configured PC release; refusing automatic source update'}
& git.exe -C $TaskPcRoot cat-file -e ($TaskApprovedCommit+'^{commit}')
if($LASTEXITCODE -ne 0) {throw 'Configured PC release is not available locally'}
if($ReadOnlyCheck) {
 [pscustomobject]@{readOnly=$true;configuredApprovedCommit=$TaskApprovedCommit;currentCommit=(& git.exe -C $TaskPcRoot rev-parse HEAD);defaultBranchUsed=$false;morningScriptExists=(Test-Path -LiteralPath (Join-Path $TaskPcRoot 'scripts\pc_morning_routine.ps1'))} | ConvertTo-Json -Compress
 exit 0
}
# Scope this Git guard to the existing morning routine; all other calls forward.
# The routine still performs its own liveness, dependency and startup checks.
function git {
 $TaskGitArguments=@($args)
 if($TaskGitArguments.Count -ge 3 -and $TaskGitArguments[0] -eq 'reset' -and $TaskGitArguments[1] -eq '--hard') {
  if($TaskGitArguments[2] -ne 'origin/master') {throw 'Unexpected startup reset target; refusing source change'}
  $TaskDirty=@(& git.exe status --porcelain)
  if($LASTEXITCODE -ne 0 -or $TaskDirty.Count) {throw 'PC checkout has local changes; refusing startup reset'}
  $TaskCurrentMorning=& git.exe rev-parse 'HEAD:scripts/pc_morning_routine.ps1'
  if($LASTEXITCODE -ne 0) {throw 'Could not inspect current startup helper'}
  $TaskApprovedMorning=& git.exe rev-parse ($TaskApprovedCommit+':scripts/pc_morning_routine.ps1')
  if($LASTEXITCODE -ne 0 -or $TaskCurrentMorning -ne $TaskApprovedMorning) {throw 'Startup helper changes require a reviewed deployment; refusing in-memory script replacement'}
  $TaskGitArguments[2]=$TaskApprovedCommit
  Write-Output ('Startup guard selects configured approved release '+$TaskApprovedCommit)
 }
 & git.exe @TaskGitArguments
}
& (Join-Path $TaskPcRoot 'scripts\pc_morning_routine.ps1')
exit $LASTEXITCODE
