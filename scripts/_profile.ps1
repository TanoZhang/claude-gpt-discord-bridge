# Dot-sourced by ensure.ps1, launch-claude.ps1 and stop.ps1. Resolves the profile the same
# way bridge\gpt_bridge.py does (use_profile) and defines:
#   $root $profilePath $configFile $cfg $runtime $data $sessionName $bridgeFile
#   Find-Bridge, Find-Claude, Start-Detached
# Without -Profile it is the single-project layout: config.json in the repository root, data\.
param([string]$Profile)

$root = Split-Path -Parent $PSScriptRoot
function Expand-Home([string]$p) {
  if ($p -match '^~') { return (Join-Path $env:USERPROFILE $p.Substring(1).TrimStart('/', '\')) }
  return $p
}

if ($Profile) {
  $item = Get-Item -LiteralPath (Expand-Home $Profile) -ErrorAction Stop
  $profilePath = $item.FullName
  if ($item.PSIsContainer) { $configFile = Join-Path $profilePath 'config.json'; $runtime = $profilePath }
  else { $configFile = $profilePath; $runtime = Join-Path $root ('data\' + $item.BaseName) }
} else {
  $profilePath = ''
  $configFile = Join-Path $root 'config.json'
  $runtime = Join-Path $root 'data'
}
$cfg = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($cfg.runtime_dir) { $runtime = [IO.Path]::GetFullPath((Expand-Home $cfg.runtime_dir)) }
if ($cfg.data_dir) { $data = [IO.Path]::GetFullPath((Expand-Home $cfg.data_dir)) }
elseif ($profilePath) { $data = Join-Path $runtime 'bridge' }
else { $data = $runtime }

$sessionName = 'Group Claude'
if ($cfg.claude.name) { $sessionName = $cfg.claude.name }
if ($cfg.claude_watch.session_name) { $sessionName = $cfg.claude_watch.session_name }
$bridgeFile = Join-Path $root 'bridge\gpt_bridge.py'

# Does a command line name this path? Case-insensitive, quoted or not, / or \.
function Test-HasPath([string]$commandLine, [string]$path) {
  if (-not $commandLine -or -not $path) { return $false }
  return $commandLine.Replace('/', '\').IndexOf($path.Replace('/', '\'), [StringComparison]::OrdinalIgnoreCase) -ge 0
}

# This profile's bridge process (not --login / --probe / --check runs).
function Find-Bridge {
  Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^pythonw?\.exe$' -and (Test-HasPath $_.CommandLine $bridgeFile) -and
    $_.CommandLine -notmatch '--(login|probe|check)' -and
    $(if ($profilePath) { Test-HasPath $_.CommandLine $profilePath } else { $_.CommandLine -notmatch '--profile' })
  }
}

# This profile's Claude channels session: claude.exe by its session name, or the launcher
# that is about to start it (so a second start in those seconds does not make a second session).
function Find-Claude {
  Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -eq 'claude.exe' -and $_.CommandLine -match '--channels' -and $_.CommandLine -like ('*' + $sessionName + '*')) -or
    ($_.Name -match '^(powershell|pwsh)\.exe$' -and $_.CommandLine -match 'launch-claude\.ps1' -and
      $(if ($profilePath) { Test-HasPath $_.CommandLine $profilePath } else { $_.CommandLine -notmatch '-Profile' }))
  }
}

# Win32_Process.Create starts the process outside the caller's process tree, so it keeps
# running after the session that ran the hook closes. ShowWindow 0 = hidden.
function Start-Detached([string]$cmd, [string]$cwd) {
  $si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmd; CurrentDirectory = $cwd; ProcessStartupInformation = $si } | Out-Null
}
