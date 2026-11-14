# Show what is running for a project and the end of its bridge log. Starts and stops nothing.
#   powershell -ExecutionPolicy Bypass -File scripts\status.ps1 [-Profile <file-or-folder>] [-Lines 8]
param([string]$Profile, [int]$Lines = 8)
. (Join-Path $PSScriptRoot '_profile.ps1') -Profile $Profile
if (-not $cfg) { exit 1 }

$bridge = @(Find-Bridge)
$claude = @(Find-Claude)
$codex  = @()
if ($bridge) {
  # Codex runs as a child of the bridge only while it works on a turn.
  $all = Get-CimInstance Win32_Process
  $ids = @($bridge | ForEach-Object { $_.ProcessId })
  $codex = @($all | Where-Object { $_.Name -eq 'codex.exe' -and $ids -contains $_.ParentProcessId })
}
function Since($p) { if ($p) { ' since ' + ($p | Sort-Object CreationDate | Select-Object -First 1).CreationDate.ToString('MM-dd HH:mm') } else { '' } }

Write-Output ("Project : {0}" -f $(if ($profilePath) { $profilePath } else { $configFile }))
Write-Output ("Bridge  : {0}{1}" -f $(if ($bridge) { 'running' } else { 'STOPPED' }), (Since $bridge))
Write-Output ("Claude  : {0}{1}  (session `"{2}`")" -f $(if ($claude) { 'running' } else { 'not running (starts at the next @Claude)' }), (Since $claude), $sessionName)
Write-Output ("Codex   : {0}" -f $(if (-not $cfg.codex.enabled) { 'off' } elseif ($codex) { 'working on a turn' } else { 'idle' }))
$log = Join-Path $data 'logs\bridge.log'
if (Test-Path -LiteralPath $log) {
  Write-Output ("Log     : {0}" -f $log)
  Get-Content -LiteralPath $log -Tail $Lines -Encoding UTF8 | ForEach-Object { '  ' + $_ }
}
