# Runs the group-chat Claude Code session in this console (ensure.ps1 starts it hidden).
#   -Resume <session-id>   continue an earlier session instead of starting fresh
#
# Reads config.json, renders prompts\claude-prompt.md into data\claude-prompt.md, and
# loads plugin-patch\allow-bots.mjs into the Discord plugin through BUN_OPTIONS so the
# GPT bot's messages reach Claude.
param([string]$Resume)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$cfg  = Get-Content (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$c    = $cfg.claude

$data = Join-Path $root 'data'
New-Item -ItemType Directory -Force $data | Out-Null
$template = Join-Path $root $(if ($c.prompt_template) { $c.prompt_template } else { 'prompts\claude-prompt.md' })
$prompt   = Join-Path $data 'claude-prompt.md'
$text = Get-Content $template -Raw -Encoding UTF8
$text = $text.Replace('{{OWNER_ID}}', $cfg.owner_id).Replace('{{GPT_BOT_ID}}', $cfg.gpt_bot_id)
$text = $text.Replace('{{CLAUDE_BOT_ID}}', $cfg.claude_bot_id).Replace('{{CHANNEL_ID}}', $cfg.channel_id)
$text = $text.Replace('{{HANDOFF_FILE}}', (Join-Path $data 'handoff.md')).Replace('{{RESTART_FILE}}', (Join-Path $data 'restart.request'))
$text = $text.Replace('{{REPORTS_DIR}}', (Join-Path $data 'reports')).Replace('{{STATUS_FILE}}', (Join-Path $data 'status.md'))
$text = $text.Replace('{{LOG_CHANNEL_ID}}', $(if ($cfg.log_channel_id) { $cfg.log_channel_id } else { '' }))
Set-Content -Path $prompt -Value $text -Encoding UTF8

$env:BUN_OPTIONS = '--preload=' + ((Join-Path $root 'plugin-patch\allow-bots.mjs') -replace '\\', '/')
$env:DISCORD_ALLOW_BOT_IDS = $cfg.gpt_bot_id
$env:DISCORD_SKIP_HUMAN_TO_BOT_IDS = $cfg.gpt_bot_id
if ($cfg.permissions_in_channel) { $env:DISCORD_PERMISSION_CHANNEL_ID = $cfg.channel_id }  # Allow/Deny buttons in the channel

$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) { $claude = Join-Path $env:USERPROFILE '.local\bin\claude.exe' }
$cliArgs = @()
if ($Resume) { $cliArgs += @('--resume', $Resume) }
$cliArgs += @(
  '--channels', 'plugin:discord@claude-plugins-official',
  '--permission-mode', $(if ($c.permission_mode) { $c.permission_mode } else { 'auto' }),
  '--model', $(if ($c.model) { $c.model } else { 'sonnet' }),
  '--name', $(if ($c.name) { $c.name } else { 'Group Claude' }),
  '--add-dir', $data,  # handoff, status and restart files: no Allow click needed for them
  '--append-system-prompt-file', $prompt)

Set-Location $c.workdir
& $claude @cliArgs
