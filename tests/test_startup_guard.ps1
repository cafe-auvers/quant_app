$ErrorActionPreference='Stop'
$TaskAst=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\start_reviewed_pc_morning.ps1'),[ref]$null,[ref]$null)
$TaskFunction=$TaskAst.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'git'},$true)
Invoke-Expression $TaskFunction.Extent.Text
$TaskApprovedCommit='a'*40
$script:TaskCalls=@()
$script:TaskDirty=$false
$script:TaskDifferentHelper=$false
function git.exe {
 $script:TaskCalls+=,@($args)
 $global:LASTEXITCODE=0
 if($args[0] -eq 'status' -and $script:TaskDirty) {return ' M main.py'}
 if($args[0] -eq 'rev-parse') {
  if($args[1].StartsWith('HEAD:') -or -not $script:TaskDifferentHelper) {return 'same-helper'}
  return 'different-helper'
 }
}
git reset --hard origin/master | Out-Null
if($TaskCalls[-1][2] -ne $TaskApprovedCommit) {throw 'Did not replace default branch with configured approved commit'}
$script:TaskCalls=@()
git fetch origin | Out-Null
if(($TaskCalls[0] -join ' ') -ne 'fetch origin') {throw 'Changed an ordinary Git command'}
$script:TaskCalls=@()
$script:TaskDirty=$true
$TaskCaught=$false
try {git reset --hard origin/master | Out-Null} catch {$TaskCaught=$true}
if(-not $TaskCaught -or @($TaskCalls | Where-Object {$_[0] -eq 'reset'}).Count) {throw 'Did not preserve dirty checkout'}
$script:TaskDirty=$false
$script:TaskCalls=@()
$TaskCaught=$false
try {git reset --hard other-branch | Out-Null} catch {$TaskCaught=$true}
if(-not $TaskCaught -or $TaskCalls.Count) {throw 'Did not reject an unexpected reset target'}
$script:TaskDifferentHelper=$true
$script:TaskCalls=@()
$TaskCaught=$false
try {git reset --hard origin/master | Out-Null} catch {$TaskCaught=$true}
if(-not $TaskCaught -or @($TaskCalls | Where-Object {$_[0] -eq 'reset'}).Count) {throw 'Did not reject an in-memory helper replacement'}
'5 startup guard checks passed; no native Git command was executed'
