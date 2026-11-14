# Start the group chat if it is not running: the GPT bridge (pythonw, no window) and the
# Claude channels session (hidden console). Idempotent, 60 s launch lock, prints nothing,
# so it can run from a Claude Code SessionStart hook:
#
#   "hooks": { "SessionStart": [ { "hooks": [ { "type": "command", "command": "powershell.exe",
#     "args": ["-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
#              "-File", "C:\\path\\to\\scripts\\ensure.ps1", "-Profile", "C:\\path\\to\\profile.json", "-BridgeOnly"],
#     "async": true, "timeout": 30 } ] } ] }
#
#   -Profile <file-or-folder>  the project to start (default: config.json in the repository root)
#   -BridgeOnly                start only the bridge; Claude then starts when someone @mentions it
#                              (needs claude_watch.wake_on_mention in the config)
param([string]$Profile, [switch]$BridgeOnly)
$ErrorActionPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot '_profile.ps1') -Profile $Profile
if (-not $cfg) { exit 1 }
New-Item -ItemType Directory -Force $runtime | Out-Null
$lock = Join-Path $runtime 'ensure.lock'

$claudeUp = $BridgeOnly -or [bool](Find-Claude)
$bridgeUp = [bool](Find-Bridge)
if ($claudeUp -and $bridgeUp) { exit 0 }

# Several sessions can start at once: skip if another launch happened in the last 60 s.
if ((Test-Path $lock) -and (((Get-Date) - (Get-Item $lock).LastWriteTime).TotalSeconds -lt 60)) { exit 0 }
Set-Content -Path $lock -Value (Get-Date -Format o)
Add-Content -Path (Join-Path $runtime 'start.log') -Value ("{0} start: claude={1} bridge={2}" -f (Get-Date -Format s), (-not $claudeUp), (-not $bridgeUp))

$profileArg = ''
if ($profilePath) { $profileArg = ' -Profile "' + $profilePath + '"' }
if (-not $claudeUp) {
  Start-Detached ("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\launch-claude.ps1`"" + $profileArg) $root
}
if (-not $bridgeUp) {
  $py = Join-Path $root '.venv\Scripts\pythonw.exe'
  if ($cfg.python) { $py = Expand-Home $cfg.python }
  if (-not (Test-Path $py)) { $py = 'pythonw.exe' }
  $cmd = "`"$py`" `"$bridgeFile`""
  if ($profilePath) { $cmd += " --profile `"$profilePath`"" }
  Start-Detached $cmd $root
}
exit 0
