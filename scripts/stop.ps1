# Stop the group chat: the GPT bridge (clean exit via stop.flag) and the Claude channels
# session. With -Restart, start again afterwards.
#   powershell -ExecutionPolicy Bypass -File scripts\stop.ps1 [-Profile <file-or-folder>] [-Restart] [-KeepClaude]
#
#   -KeepClaude  leave the Claude session running (stop or restart the bridge only)
# With -Restart, Claude is started again right away unless the config sets
# claude_watch.wake_on_mention: then it starts at the next @Claude.
# Check that nobody is mid-task first: a restarted Claude session starts fresh
# (use launch-claude.ps1 -Resume <session-id> to continue an old one).
param([string]$Profile, [switch]$Restart, [switch]$KeepClaude)
. (Join-Path $PSScriptRoot '_profile.ps1') -Profile $Profile
if (-not $cfg) { exit 1 }
$flag = Join-Path $data 'stop.flag'

$wasRunning = [bool](Find-Bridge)
if ($wasRunning) { New-Item -ItemType File -Force $flag | Out-Null }
# /T: with the console that hosts the session and whatever it started
if (-not $KeepClaude) { Find-Claude | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F 2>$null | Out-Null } }

for ($i = 0; $i -lt 30 -and ((Find-Bridge) -or ((-not $KeepClaude) -and (Find-Claude))); $i++) { Start-Sleep -Seconds 1 }
# Still there after 30 s: end it with its children (a running Codex turn), or they would be orphaned.
Find-Bridge | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F 2>$null | Out-Null }
Remove-Item $flag -ErrorAction SilentlyContinue
if (Find-Bridge) { Write-Output 'The bridge is STILL RUNNING: it could not be stopped.'; exit 1 }
elseif ($wasRunning) { Write-Output 'Group chat stopped.' }
else { Write-Output 'No bridge was running for this profile.' }

if ($Restart) {
  Remove-Item (Join-Path $runtime 'ensure.lock') -ErrorAction SilentlyContinue
  $bridgeOnly = $KeepClaude -or [bool]$cfg.claude_watch.wake_on_mention
  & (Join-Path $PSScriptRoot 'ensure.ps1') -Profile $Profile -BridgeOnly:$bridgeOnly
  if ($bridgeOnly) { Write-Output 'Bridge starting. Claude starts at the next @Claude.' }
  else { Write-Output 'Group chat starting (Claude takes ~30 s to come online).' }
}
