# Start the group chat if it is not running: the Claude channels session (hidden
# console) and the GPT bridge (pythonw, no window). Idempotent, 60 s launch lock,
# prints nothing, so it can run from a Claude Code SessionStart hook:
#
#   "hooks": { "SessionStart": [ { "hooks": [ { "type": "command", "command": "powershell.exe",
#     "args": ["-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
#              "-File", "C:\\path\\to\\claude-gpt-discord-bridge\\scripts\\ensure.ps1"],
#     "async": true, "timeout": 30 } ] } ] }
$ErrorActionPreference = 'SilentlyContinue'
$root   = Split-Path -Parent $PSScriptRoot
$data   = Join-Path $root 'data'
New-Item -ItemType Directory -Force $data | Out-Null
$lock   = Join-Path $data 'ensure.lock'
$prompt = [regex]::Escape((Join-Path $data 'claude-prompt.md'))
$bridge = [regex]::Escape((Join-Path $root 'bridge\gpt_bridge.py'))

$procs    = Get-CimInstance Win32_Process
$claudeUp = [bool]($procs | Where-Object { $_.Name -eq 'claude.exe' -and $_.CommandLine -match $prompt })
$bridgeUp = [bool]($procs | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match $bridge -and $_.CommandLine -notmatch '--(login|probe)' })
if ($claudeUp -and $bridgeUp) { exit 0 }

if ((Test-Path $lock) -and (((Get-Date) - (Get-Item $lock).LastWriteTime).TotalSeconds -lt 60)) { exit 0 }
Set-Content -Path $lock -Value (Get-Date -Format o)
Add-Content -Path (Join-Path $data 'start.log') -Value ("{0} start: claude={1} bridge={2}" -f (Get-Date -Format s), (-not $claudeUp), (-not $bridgeUp))

# Win32_Process.Create starts the process outside the caller's process tree, so it
# keeps running after the session that ran the hook closes. ShowWindow 0 = hidden.
function Start-Detached([string]$cmd, [string]$cwd) {
  $si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmd; CurrentDirectory = $cwd; ProcessStartupInformation = $si } | Out-Null
}

if (-not $claudeUp) {
  Start-Detached "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\launch-claude.ps1`"" $root
}
if (-not $bridgeUp) {
  $py = Join-Path $root '.venv\Scripts\pythonw.exe'
  if (-not (Test-Path $py)) { $py = 'pythonw.exe' }
  Start-Detached "`"$py`" `"$root\bridge\gpt_bridge.py`"" $root
}
exit 0
