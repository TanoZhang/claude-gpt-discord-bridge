# Stop the group chat: the GPT bridge (clean exit via data\stop.flag) and the Claude
# channels session. With -Restart, start both again.
#   powershell -ExecutionPolicy Bypass -File scripts\stop.ps1 [-Restart]
# Check that nobody is mid-task first: a restarted Claude session starts fresh
# (use launch-claude.ps1 -Resume <session-id> to continue an old one).
param([switch]$Restart)
$root   = Split-Path -Parent $PSScriptRoot
$data   = Join-Path $root 'data'
$prompt = [regex]::Escape((Join-Path $data 'claude-prompt.md'))
$bridgePath = [regex]::Escape((Join-Path $root 'bridge\gpt_bridge.py'))

$bridge = { Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match $bridgePath } }
$claude = { Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'claude.exe' -and $_.CommandLine -match $prompt } }

if (& $bridge) { New-Item -ItemType File -Force (Join-Path $data 'stop.flag') | Out-Null }
& $claude | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

for ($i = 0; $i -lt 30 -and ((& $bridge) -or (& $claude)); $i++) { Start-Sleep -Seconds 1 }
& $bridge | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item (Join-Path $data 'stop.flag') -ErrorAction SilentlyContinue
Write-Output 'Group chat stopped.'

if ($Restart) {
  Remove-Item (Join-Path $data 'ensure.lock') -ErrorAction SilentlyContinue
  & (Join-Path $PSScriptRoot 'ensure.ps1')
  Write-Output 'Group chat starting (Claude takes ~30 s to come online).'
}
