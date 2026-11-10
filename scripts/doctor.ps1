<#
.SYNOPSIS
  Diagnose a harvesting-dune setup: every link from the companion to the minimap lines is
  checked, and each problem comes with the fix.

.DESCRIPTION
  Run from the repository folder (safe at any time, changes nothing):
      powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1

  -Report also writes everything to data\diagnostics-<date>.txt to share when asking for help
  (it contains paths on your PC and recent log lines, no passwords or tokens).
#>
param(
    [int]$Port = 9231,
    [int]$HttpPort = 8765,
    [switch]$Report
)
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'companion-path.ps1')
. (Join-Path $PSScriptRoot 'uv-setup.ps1')

$results = New-Object System.Collections.Generic.List[object]
$lines = New-Object System.Collections.Generic.List[string]
function Out-Line([string]$text, [string]$color = 'Gray') {
    Write-Host $text -ForegroundColor $color
    $lines.Add($text)
}
function Check([string]$name, [string]$status, [string]$detail, [string]$fix = '') {
    $color = @{ PASS = 'Green'; WARN = 'Yellow'; FAIL = 'Red'; INFO = 'Cyan' }[$status]
    Out-Line ('[{0}] {1}: {2}' -f $status, $name, $detail) $color
    if ($fix -and $status -ne 'PASS') { Out-Line ('       fix: ' + $fix) 'White' }
    $results.Add([pscustomobject]@{ Name = $name; Status = $status })
}
function Get-Json([string]$url) {
    try { return Invoke-RestMethod -Uri $url -TimeoutSec 3 -UseBasicParsing } catch { return $null }
}
function Get-PortOwner([int]$p) {
    try {
        $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop | Select-Object -First 1
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) { return $proc.ProcessName } else { return "pid $($c.OwningProcess)" }
    } catch { return $null }
}
$installCmd = 'powershell -ExecutionPolicy Bypass -File scripts\install.ps1'

Out-Line "harvesting-dune doctor  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" 'Cyan'
Out-Line "folder: $repo"
Out-Line ''

# -- system ---------------------------------------------------------------------------------
$os = [Environment]::OSVersion.Version
if ($os.Major -ge 10) { Check 'Windows' 'PASS' "Windows $($os.Major).$($os.Build)" }
else { Check 'Windows' 'FAIL' "Windows $os is too old" 'Use Windows 10 or Windows 11.' }
Check 'PowerShell' 'INFO' "$($PSVersionTable.PSVersion) ($($ExecutionContext.SessionState.LanguageMode))"
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    Check 'PowerShell mode' 'FAIL' 'restricted language mode (set by an administrator or security software)' `
        'Ask the PC administrator to allow PowerShell scripts, or run on a PC without this restriction.'
}
if ($repo -match '\\AppData\\Local\\Temp\\' -or $repo -match '\.zip\\') {
    Check 'Folder location' 'FAIL' 'running from a temporary or ZIP folder' 'Extract the folder to a permanent location, for example Documents\harvesting-dune. Then run the installer there.'
}
try {
    New-Item -ItemType Directory -Force -Path (Join-Path $repo 'data') | Out-Null
    $probe = Join-Path $repo 'data\.write-test'
    Set-Content -LiteralPath $probe -Value 'ok' -ErrorAction Stop
    Remove-Item -LiteralPath $probe -Force
    Check 'Folder writable' 'PASS' 'data\ can be written'
} catch {
    Check 'Folder writable' 'FAIL' "cannot write to $repo\data ($($_.Exception.Message))" 'Move the folder to a location that you own, for example Documents. If the problem continues, examine the antivirus setting "controlled folder access".'
}

# -- companion -------------------------------------------------------------------------------
$companion = Find-Companion
if ($companion) { Check 'Companion installed' 'PASS' $companion }
else { Check 'Companion installed' 'FAIL' 'gaming.tools companion not found' 'Install the gaming.tools companion from https://gaming.tools. Then open its Dune module one time.' }

# -- detector environment ----------------------------------------------------------------------
$uv = Find-Uv
if ($uv) { Check 'uv' 'PASS' $uv }
else { Check 'uv' 'WARN' 'not found (the installer needs it to install or update)' 'Run Install.cmd again. It installs uv.' }
$exe = Join-Path $repo '.venv\Scripts\harvesting-dune.exe'
if (Test-Path -LiteralPath $exe) {
    $out = & $exe --help 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) { Check 'Detector program' 'PASS' $exe }
    else { Check 'Detector program' 'FAIL' "it does not start: $($out.Trim() -split "`n" | Select-Object -Last 1)" "Reinstall: $installCmd" }
} else {
    Check 'Detector program' 'FAIL' '.venv\Scripts\harvesting-dune.exe is missing' "Run $installCmd"
}

# -- DevTools policy ---------------------------------------------------------------------------
$want = "--remote-debugging-port=$Port"
$userPolicy = (Get-ItemProperty 'HKCU:\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments' -ErrorAction SilentlyContinue).'GT.Launcher.exe'
$machinePolicy = (Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments' -ErrorAction SilentlyContinue).'GT.Launcher.exe'
if ($machinePolicy -and $machinePolicy -ne $want) {
    Check 'DevTools setting' 'FAIL' "a machine-wide policy overrides ours: '$machinePolicy'" "Ask an administrator to change HKLM\SOFTWARE\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments\GT.Launcher.exe to '$want', or to remove it."
} elseif ($userPolicy -eq $want -or $machinePolicy -eq $want) {
    Check 'DevTools setting' 'PASS' "companion launches get $want"
} elseif ($userPolicy) {
    Check 'DevTools setting' 'FAIL' "set to '$userPolicy', expected '$want'" "Run $installCmd"
} else {
    Check 'DevTools setting' 'FAIL' 'not set: the companion will not expose its map data' "Run $installCmd"
}
if ($env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS) {
    Check 'WebView2 env var' 'WARN' "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS='$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS' is set and can replace the setting" 'If you did not set this environment variable on purpose, remove it (user or system).'
}

# -- autorun task and watcher ---------------------------------------------------------------------
$task = Get-ScheduledTask -TaskName 'harvesting-dune autorun' -ErrorAction SilentlyContinue
$watcherScript = Join-Path $PSScriptRoot 'companion-autorun.ps1'
if (-not $task) {
    Check 'Autorun task' 'FAIL' 'not registered' "Run $installCmd"
} else {
    $args0 = ($task.Actions | Select-Object -First 1).Arguments
    $info = Get-ScheduledTaskInfo -TaskName 'harvesting-dune autorun' -ErrorAction SilentlyContinue
    # IndexOf, not -notlike: a path with [ ] would be read as a wildcard class and never match itself
    if (([string]$args0).IndexOf($watcherScript, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        Check 'Autorun task' 'FAIL' 'points to a different folder (the folder was possibly moved or copied)' "Run $installCmd from this folder."
    } elseif ($task.State -eq 'Disabled') {
        Check 'Autorun task' 'FAIL' 'disabled' 'Run: Enable-ScheduledTask -TaskName "harvesting-dune autorun"'
    } else {
        Check 'Autorun task' 'PASS' "state $($task.State), last result $($info.LastTaskResult)"
    }
}
$watcher = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'companion-autorun\.ps1' -and $_.Name -match 'powershell' }
if ($watcher) { Check 'Watcher running' 'PASS' "pid $(@($watcher)[0].ProcessId)" }
elseif ($task) { Check 'Watcher running' 'FAIL' 'the autorun task is not running' 'Run: Start-ScheduledTask -TaskName "harvesting-dune autorun". As an alternative, sign out and sign in again.' }

# -- live chain: companion -> port -> pages -> detector ---------------------------------------------
$gt = Get-Process -Name 'GT.Launcher' -ErrorAction SilentlyContinue
$version = Get-Json "http://127.0.0.1:$Port/json/version"
if (-not $gt) {
    Check 'Companion running' 'INFO' 'not running (open it to start the detector)'
    $owner = Get-PortOwner $Port
    if ($owner) { Check "Port $Port" 'FAIL' "used by '$owner' while the companion is closed" "Close '$owner', or reinstall with another port: $installCmd  (edit -Port)" }
} else {
    Check 'Companion running' 'PASS' "pid $(@($gt)[0].Id)"
    if ($version) {
        Check 'DevTools port' 'PASS' "127.0.0.1:$Port answers"
        # WHY assign first: Windows PowerShell 5.1's Invoke-RestMethod emits a JSON array as one object, so piping the call filters the whole array at once
        $list = Get-Json "http://127.0.0.1:$Port/json/list"
        $pages = @($list | Where-Object { $_.type -eq 'page' -and $_.url -like '*gaming.tools*' })
        $dune = @($pages | Where-Object { $_.url -match '/dune' })
        if ($dune.Count -gt 0) { Check 'Companion Dune pages' 'PASS' (($dune | ForEach-Object { $_.url }) -join ', ') }
        else { Check 'Companion Dune pages' 'WARN' 'no Dune map or minimap page open' 'In the companion, open Dune: Awakening (the map or the minimap overlay).' }
    } else {
        Check 'DevTools port' 'FAIL' "the companion runs but port $Port does not answer" 'Close the companion completely (tray icon > Exit). Then open it again. The companion must start after the installation.'
    }
}

$state = Get-Json "http://127.0.0.1:$HttpPort/state"
$det = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'harvesting-dune(\.exe)?"?\s+run' } | Select-Object -First 1
if ($state) {
    if ($state.connected) { Check 'Detector' 'PASS' "connected, world $($state.worldName), $($state.scanRows) scan rows, searching '$($state.resource)'" }
    else { Check 'Detector' 'WARN' 'running but no data from the companion yet' 'Start the game. In the companion, open the Dune map or the minimap.' }
    Check 'Detector status' 'INFO' $state.status
    if ($state.minimap -and $state.minimap.lines -and -not $state.minimap.linesActive) {
        Check 'Lines' 'WARN' "the minimap shows $($state.minimap.visibleCount) resources" 'On the settings page, tick exactly one resource.'
    }
} elseif ($det) {
    $owner = Get-PortOwner $HttpPort
    if ($owner -and $owner -notmatch 'python|dune') { Check 'Settings page' 'FAIL' "port $HttpPort is used by '$owner'" "Close '$owner'. As an alternative, start the detector with --http-port <other port>." }
    else { Check 'Detector' 'WARN' 'process running but the settings page does not answer (starting, or stuck)' 'Wait 30 s. Then run the doctor again. If the problem continues, close the companion and open it again.' }
} elseif ($gt) {
    Check 'Detector' 'FAIL' 'not running although the companion is' 'Read data\autorun.log below. Close the companion and open it again. If it fails again, run the installer again.'
}

# -- logs ----------------------------------------------------------------------------------------
function Show-Log([string]$rel, [int]$tail, [string]$pattern = '') {
    $path = Join-Path $repo $rel
    if (-not (Test-Path -LiteralPath $path)) { return }
    $content = Get-Content -LiteralPath $path -ErrorAction SilentlyContinue
    if ($pattern) { $content = $content | Where-Object { $_ -match $pattern } }
    $content = @($content | Select-Object -Last $tail)
    if ($content.Count) {
        Out-Line ''
        Out-Line "--- $rel$(if ($pattern) { ' (warnings and errors)' })" 'Cyan'
        foreach ($l in $content) { Out-Line "  $l" }
    }
}
Show-Log 'data\autorun.log' 8
Show-Log 'data\logs\errors.log' 15
Show-Log 'data-run.log' 12 'WARNING|ERROR|Traceback|Error|failed|crashed'
Show-Log 'data-run.prev.log' 15 'WARNING|ERROR|Traceback|Error|failed|crashed'

$fails = @($results | Where-Object Status -eq 'FAIL').Count
$warns = @($results | Where-Object Status -eq 'WARN').Count
Out-Line ''
if ($fails) { Out-Line "$fails problem(s) found. Do the first 'fix' above. Then run the doctor again." 'Red' }
elseif ($warns) { Out-Line "No problems that stop the tool. $warns warning(s)." 'Yellow' }
else { Out-Line 'Everything checks out.' 'Green' }

if ($Report) {
    $file = Join-Path $repo ("data\diagnostics-{0}.txt" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $lines | Set-Content -LiteralPath $file -Encoding UTF8
    Write-Host "Report written to $file"
}
exit $(if ($fails) { 1 } else { 0 })
