<#
.SYNOPSIS
  Keeps one WSL2 distro running while you are logged on to Windows, so ccboard's tmux sessions survive closing every terminal (issue #118).

.DESCRIPTION
  WSL2 stops its virtual machine a few minutes after the last terminal closes, and systemd services inside the distro do NOT keep it alive.
  Stopping the machine ends the tmux server and every agent session with it. This script registers a per-user Scheduled Task, "At log on",
  that holds one hidden wsl.exe process open for the distro you name. It needs no administrator rights and changes nothing else.

  Run it from Windows PowerShell (not from inside the distro), after copying it out of the checkout, for example:
      powershell -NoProfile -ExecutionPolicy Bypass -File .\ccboard-wsl-keepalive.ps1 -Distro Ubuntu

  Safe to run again: the task is replaced, not duplicated. -Remove unregisters it.
  Windows starts WSL after you log on, not before: after a Windows restart the board is back once you have logged on (or enable Windows auto-logon).

.PARAMETER Distro
  The distro name as `wsl -l -q` lists it. Default: the first one listed (the default distro). A name that is not installed prints a message and exits 0.

.PARAMETER Method
  sleep (default): the task runs `wsl.exe -d <distro> --exec sleep infinity` in a hidden window and keeps running. Follows from Microsoft's FAQ
  (the machine stays up while a Windows process holds a handle to it); that this holds on every WSL version is to verify.
  dbus: the task runs `wsl.exe -d <distro> --exec dbus-launch true` (the community recipe; needs the `dbus` package in the distro).

.PARAMETER Remove
  Unregister the task and stop. Touches nothing else, not even .wslconfig.

.PARAMETER WriteWslConfig
  Also write the belt-and-braces lines into %UserProfile%\.wslconfig, after copying the file to .wslconfig.ccboard-bak-<time>. Without this switch
  the lines are only printed. They are written only into the keys shown, existing settings are kept. WSL reads them at its next full stop
  (`wsl --shutdown`, which ends every running session: do it when nothing is running).

.PARAMETER Mirrored
  With the .wslconfig lines, also use networkingMode=mirrored (Windows 11 22H2 or newer). Leave it out on Windows 10.
#>
[CmdletBinding()]
param(
  [ValidatePattern('^[A-Za-z0-9._-]+$')]
  [string]$Distro,

  [ValidateSet('sleep', 'dbus')]
  [string]$Method = 'sleep',

  [switch]$Remove,
  [switch]$WriteWslConfig,
  [switch]$Mirrored
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ccboard-wsl-keepalive'

function Say([string]$Text) { Write-Host $Text }

# Unregister the task when it exists. Returns $true when there was one.
function Remove-KeepAliveTask {
  $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $existing) { return $false }
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  return $true
}

if ($Remove) {
  if (Remove-KeepAliveTask) { Say "Removed the scheduled task $TaskName. WSL stops the distro on its own again a few minutes after the last terminal closes." }
  else { Say "There is no scheduled task named $TaskName. Nothing to remove." }
  exit 0
}

# wsl.exe prints UTF-16; read it as such, then drop empty lines.
function Get-WslDistros {
  $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
  if (-not $wsl) { return $null }
  $previous = [Console]::OutputEncoding
  try {
    [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
    $lines = & wsl.exe -l -q 2>$null
  } finally {
    [Console]::OutputEncoding = $previous
  }
  return @($lines | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
}

$distros = Get-WslDistros
if ($null -eq $distros) { Say 'wsl.exe was not found. Install WSL first: wsl --install -d Ubuntu. Nothing was changed.'; exit 0 }
if ($distros.Count -eq 0) { Say 'No WSL distro is installed (wsl -l -q lists none). Install one: wsl --install -d Ubuntu. Nothing was changed.'; exit 0 }
if (-not $Distro) { $Distro = $distros[0] }
if ($distros -notcontains $Distro) {
  Say "No distro named '$Distro' is installed. Installed: $($distros -join ', '). Nothing was changed."
  exit 0
}

# ---- the task: per user, at log on, no administrator rights (the principal is you, run level Limited)
if ($Method -eq 'dbus') { $inner = "wsl.exe -d $Distro --exec dbus-launch true" }
else { $inner = "wsl.exe -d $Distro --exec sleep infinity" }

$me = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -Command "& ' + $inner + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

$replaced = Remove-KeepAliveTask
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
  -Description "Keeps the WSL distro $Distro running so ccboard's tmux sessions survive (method $Method)." | Out-Null
Start-ScheduledTask -TaskName $TaskName
if ($replaced) { Say "Replaced the scheduled task $TaskName (distro $Distro, method $Method) and started it." }
else { Say "Registered the scheduled task $TaskName (distro $Distro, method $Method) and started it." }
Say 'It runs at every log on. Remove it with: -Remove'

# ---- the belt-and-braces lines for %UserProfile%\.wslconfig
$wanted = @(
  @{ Section = 'general'; Key = 'instanceIdleTimeout'; Value = '-1' },
  @{ Section = 'wsl2'; Key = 'vmIdleTimeout'; Value = '-1' }
)
if ($Mirrored) { $wanted += @{ Section = 'wsl2'; Key = 'networkingMode'; Value = 'mirrored' } }

# Pure text in, text out: sets Key=Value inside [Section], keeping every other line. Writes nothing.
function Merge-IniValue([string]$Text, [string]$Section, [string]$Key, [string]$Value) {
  $lines = New-Object System.Collections.Generic.List[string]
  if ($Text) { foreach ($l in ($Text -split "\r?\n")) { $lines.Add($l) } }
  while ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq '') { $lines.RemoveAt($lines.Count - 1) }
  $start = -1
  for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim() -ieq "[$Section]") { $start = $i; break } }
  if ($start -lt 0) {
    if ($lines.Count -gt 0) { $lines.Add('') }
    $lines.Add("[$Section]"); $lines.Add("$Key=$Value")
    return ($lines -join "`r`n") + "`r`n"
  }
  $end = $lines.Count
  for ($i = $start + 1; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim() -match '^\[.*\]$') { $end = $i; break } }
  for ($i = $start + 1; $i -lt $end; $i++) {
    if ($lines[$i] -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) { $lines[$i] = "$Key=$Value"; return ($lines -join "`r`n") + "`r`n" }
  }
  $lines.Insert($end, "$Key=$Value")
  return ($lines -join "`r`n") + "`r`n"
}

$configPath = Join-Path $env:USERPROFILE '.wslconfig'
Say ''
Say "These lines belong in $configPath (they keep WSL from stopping the distro when it looks idle):"
foreach ($w in $wanted) { Say ("  [{0}] {1}={2}" -f $w.Section, $w.Key, $w.Value) }

if ($WriteWslConfig) {
  $current = ''
  if (Test-Path -LiteralPath $configPath) {
    $backup = "$configPath.ccboard-bak-" + (Get-Date -Format 'yyyyMMddHHmmss')
    Copy-Item -LiteralPath $configPath -Destination $backup
    Say "Backed up the existing file to $backup"
    $current = Get-Content -LiteralPath $configPath -Raw
  }
  $merged = $current
  foreach ($w in $wanted) { $merged = Merge-IniValue $merged $w.Section $w.Key $w.Value }
  Set-Content -LiteralPath $configPath -Value $merged -Encoding ASCII -NoNewline
  Say "Wrote $configPath. WSL reads it after a full stop: run 'wsl --shutdown' when nothing is running (it ends every session)."
} else {
  Say 'Nothing was written. Add -WriteWslConfig to write them (a backup is made first).'
}
Say 'Even with these set, the keep-alive task is what holds the distro open; behaviour on WSL 3.x is UNVERIFIED.'
exit 0
