# Runs the group-chat Claude Code session in this console (ensure.ps1 starts it hidden).
#   -Profile <file-or-folder>  the project (default: config.json in the repository root)
#   -Resume <session-id>       continue an earlier session instead of starting fresh
#
# Reads the config, takes the system prompt (claude.prompt_file as it is, or
# claude.prompt_template rendered into <runtime>\claude-prompt.md), and loads
# plugin-patch\allow-bots.mjs into the Discord plugin through BUN_OPTIONS so the other
# bots' messages reach Claude. Output goes to <runtime>\logs\claude-run.log.
#
# Optional config keys under "claude":
#   state_dir          DISCORD_STATE_DIR for the plugin (its own access.json per project)
#   token_file         a file with DISCORD_BOT_TOKEN=... when state_dir has no .env of its own
#   access_mode        DISCORD_ACCESS_MODE, e.g. "static"
#   permission_wording wording of the Allow/Deny post (see plugin-patch\allow-bots.mjs)
#   name_format        session title with the start time: {M} month, {d} day, {H} hour, {name} the session name
param([string]$Profile, [string]$Resume)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_profile.ps1') -Profile $Profile
$c = $cfg.claude

New-Item -ItemType Directory -Force (Join-Path $runtime 'logs') | Out-Null
Start-Transcript -LiteralPath (Join-Path $runtime 'logs\claude-run.log') -Append | Out-Null

if ($c.prompt_file) {
  $prompt = [IO.Path]::GetFullPath((Expand-Home $c.prompt_file))
} else {
  $template = Join-Path $root $(if ($c.prompt_template) { $c.prompt_template } else { 'prompts\claude-prompt.md' })
  $prompt   = Join-Path $runtime 'claude-prompt.md'
  $text = Get-Content $template -Raw -Encoding UTF8
  $text = $text.Replace('{{OWNER_ID}}', $cfg.owner_id).Replace('{{GPT_BOT_ID}}', $cfg.gpt_bot_id)
  $text = $text.Replace('{{CLAUDE_BOT_ID}}', $cfg.claude_bot_id).Replace('{{CHANNEL_ID}}', $cfg.channel_id)
  $text = $text.Replace('{{HANDOFF_FILE}}', (Join-Path $runtime 'handoff.md')).Replace('{{RESTART_FILE}}', (Join-Path $runtime 'restart.request')).Replace('{{MODEL_FILE}}', (Join-Path $runtime 'claude-model.json'))
  $text = $text.Replace('{{REPORTS_DIR}}', (Join-Path $runtime 'reports')).Replace('{{STATUS_FILE}}', (Join-Path $runtime 'status.md'))
  $text = $text.Replace('{{LOG_CHANNEL_ID}}', $(if ($cfg.log_channel_id) { $cfg.log_channel_id } else { '' }))
  Set-Content -Path $prompt -Value $text -Encoding UTF8
}

# Bots whose messages Claude may read: GPT, and Codex and Gemini when they are members of the group.
$bots = @($cfg.gpt_bot_id)
if ($cfg.codex.enabled -and $cfg.codex.bot_id) { $bots += $cfg.codex.bot_id }
if ($cfg.gemini.enabled -and $cfg.gemini.bot_id) { $bots += $cfg.gemini.bot_id }
$bots = ($bots | Where-Object { $_ }) -join ','
$env:BUN_OPTIONS = '--preload=' + ((Join-Path $root 'plugin-patch\allow-bots.mjs') -replace '\\', '/')
$env:DISCORD_ALLOW_BOT_IDS = $bots
$env:DISCORD_SKIP_HUMAN_TO_BOT_IDS = $bots  # a human message meant only for those bots skips Claude
if ($cfg.permissions_in_channel) { $env:DISCORD_PERMISSION_CHANNEL_ID = $cfg.channel_id }  # Allow/Deny buttons in the channel
if ($c.permission_wording) { $env:DISCORD_PERMISSION_WORDING = ($c.permission_wording | ConvertTo-Json -Compress -Depth 4) }
if ($c.state_dir) { $env:DISCORD_STATE_DIR = [IO.Path]::GetFullPath((Expand-Home $c.state_dir)) }
if ($c.access_mode) { $env:DISCORD_ACCESS_MODE = $c.access_mode }
if ($c.token_file) {
  $line = Get-Content -LiteralPath (Expand-Home $c.token_file) | Where-Object { $_ -match '^\s*DISCORD_BOT_TOKEN\s*=' } | Select-Object -First 1
  if (-not $line) { throw 'DISCORD_BOT_TOKEN is missing in claude.token_file.' }
  $env:DISCORD_BOT_TOKEN = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
}

# After an idle stop (claude_watch.idle_stop_min) the bridge leaves the session id here:
# continue that conversation once instead of starting from nothing.
$lastFile = Join-Path $runtime 'claude-last-session.txt'
if (-not $Resume -and (Test-Path -LiteralPath $lastFile)) {
  $last = (Get-Content -LiteralPath $lastFile -Raw).Trim()
  Remove-Item -LiteralPath $lastFile -ErrorAction SilentlyContinue
  $projectDir = Join-Path $env:USERPROFILE ('.claude\projects\' + ($c.workdir -replace '[^A-Za-z0-9]', '-'))
  if ($cfg.claude_watch.project_dir) { $projectDir = Expand-Home $cfg.claude_watch.project_dir }
  if ($last -and (Test-Path -LiteralPath (Join-Path $projectDir ($last + '.jsonl')))) { $Resume = $last }
}

# claude.name_format puts the session's start time in front of its name, e.g. "{M}/{d} {H}h {name}".
# The name itself must stay in the title: the bridge and these scripts find the session by it.
$title = $sessionName
if ($c.name_format) {
  $t = Get-Date
  if ($Resume) {  # a continued session keeps the time it first started
    $pd = Join-Path $env:USERPROFILE ('.claude\projects\' + ($c.workdir -replace '[^A-Za-z0-9]', '-'))
    if ($cfg.claude_watch.project_dir) { $pd = Expand-Home $cfg.claude_watch.project_dir }
    $old = Join-Path $pd ($Resume + '.jsonl')
    if (Test-Path -LiteralPath $old) { $t = (Get-Item -LiteralPath $old).CreationTime }
  }
  $title = ([string]$c.name_format).Replace('{M}', $t.Month).Replace('{d}', $t.Day).Replace('{H}', $t.Hour).Replace('{name}', $sessionName)
  if ($title.IndexOf($sessionName) -lt 0) { $title = $title + ' ' + $sessionName }
}

$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) { $claude = Join-Path $env:USERPROFILE '.local\bin\claude.exe' }
# Model and effort: claude.model / claude.effort, unless Claude switched them on the owner's
# instruction by writing <runtime>\claude-model.json ({"model": "...", "effort": "..."}).
$model = $(if ($c.model) { $c.model } else { 'sonnet' })
$effort = $c.effort
$switchFile = Join-Path $runtime 'claude-model.json'
if (Test-Path -LiteralPath $switchFile) {
  try {
    $sw = Get-Content -LiteralPath $switchFile -Raw | ConvertFrom-Json
    if ($sw.model -and $sw.model -ne 'default') { $model = $sw.model }
    if ($sw.effort -and $sw.effort -ne 'default') { $effort = $sw.effort }
  } catch { Write-Warning "ignoring ${switchFile}: $_" }
}
$cliArgs = @()
if ($Resume) { $cliArgs += @('--resume', $Resume) }
$cliArgs += @(
  '--channels', 'plugin:discord@claude-plugins-official',
  '--permission-mode', $(if ($c.permission_mode) { $c.permission_mode } else { 'auto' }),
  '--model', $model,
  '--name', $title,
  '--add-dir', $runtime,  # handoff, status, restart and model files: no Allow click needed for them
  '--append-system-prompt-file', $prompt)
if ($effort) { $cliArgs += @('--effort', $effort) }

Set-Location -LiteralPath $c.workdir
& $claude @cliArgs
