<#
.SYNOPSIS
  Runs harvesting-dune whenever the gaming.tools companion runs, and stops it when the
  companion exits.

.DESCRIPTION
  Meant to run hidden at logon (see install-autorun.ps1). Polls every 2 s:
    * companion up, DevTools feed ready, no detector  -> start the detector
    * detector exited while the companion is still up  -> restart it (backoff 10 s, growing
                                                          to 5 min if it keeps dying at once)
    * companion gone                                   -> the detector saves and exits by itself
                                                          (--exit-when-companion-gone); force-
                                                          stopped only if still running after 30 s
  The previous run's log is kept as data-run.prev.log, so a crash is not overwritten by the restart.
  Once per companion session, the settings page (http://127.0.0.1:8765) opens in the
  default browser as soon as the detector answers (-NoBrowser to skip).
  The companion gets its DevTools port from the per-app WebView2 policy that
  install-autorun.ps1 writes, so it works however the companion is opened.
  Log: data\autorun.log. Detector output: data-run.log (as before).
#>
param(
    [int]$Port = 9231,
    [int]$HttpPort = 8765,
    [int]$PollSeconds = 2,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Continue'

$repo = Split-Path -Parent $PSScriptRoot
$detectorExe = Join-Path $repo '.venv\Scripts\harvesting-dune.exe'
$logFile = Join-Path $repo 'data\autorun.log'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFile) | Out-Null

$mutex = New-Object System.Threading.Mutex($false, 'Local\harvesting-dune-autorun')
if (-not $mutex.WaitOne(0)) { exit 0 }  # another watcher is already running

function Write-Log([string]$msg) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $msg
    try { Add-Content -LiteralPath $logFile -Value $line -Encoding utf8 -ErrorAction Stop } catch { }
}

function Test-Feed {
    try {
        $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/list" -TimeoutSec 2
        return @($targets | Where-Object { $_.type -eq 'page' -and $_.url -like '*gaming.tools*' }).Count -gt 0
    } catch { return $false }
}

function Get-SettingsState {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$HttpPort/state" -TimeoutSec 2 -UseBasicParsing }
    catch { return $null }
}

function Get-DetectorProcess {
    # Includes one started by hand, so we never run two.
    Get-CimInstance Win32_Process -Filter "Name='harvesting-dune.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'harvesting-dune(\.exe)?"?\s+run' -or $_.CommandLine -match 'harvesting_dune.*\srun' } |
        Select-Object -First 1
}

function Show-Problem([string]$text) {
    # A message box in its own hidden process: the watcher keeps running while it is open.
    $cmd = "(New-Object -ComObject WScript.Shell).Popup('" + $text.Replace("'", "''") + "', 0, 'harvesting-dune', 4144) | Out-Null"  # 48 warning + 4096 on top of a fullscreen game
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-WindowStyle', 'Hidden', '-EncodedCommand', $encoded -WindowStyle Hidden
    } catch { Write-Log "could not show the problem message: $($_.Exception.Message)" }
}

function Stop-Detector {
    $p = Get-DetectorProcess
    while ($p) {
        & taskkill.exe /PID $p.ProcessId /T /F *> $null
        Start-Sleep -Milliseconds 300
        $p = Get-DetectorProcess
    }
}

if ((Test-Path -LiteralPath $logFile) -and (Get-Item -LiteralPath $logFile).Length -gt 1MB) {
    Remove-Item -LiteralPath $logFile -Force
}
Write-Log "watcher started (port $Port)"

$nextStart = [datetime]::MinValue
$startedAt = $null
$quickDeaths = 0
$goneSince = $null
$noFeedSince = $null
$warnedNoFeed = $false
$warnedExe = $false
$warnedCrash = $false
$browserPending = -not $NoBrowser

while ($true) {
  try {
    $companion = Get-Process -Name 'GT.Launcher' -ErrorAction SilentlyContinue
    $detector = Get-DetectorProcess

    if ($companion) {
        $goneSince = $null
        if (-not $detector -and $startedAt) {
            # it exited while the companion is up: a crash. Back off harder if it keeps happening.
            $ranFor = ((Get-Date) - $startedAt).TotalSeconds
            $quickDeaths = if ($ranFor -lt 120) { $quickDeaths + 1 } else { 0 }
            $wait = [Math]::Min(300, 10 * [Math]::Pow(2, [Math]::Max(0, $quickDeaths - 1)))
            $nextStart = (Get-Date).AddSeconds($wait)
            Write-Log ("detector exited after {0:N0} s; restarting in {1:N0} s (see data-run.prev.log)" -f $ranFor, $wait)
            $startedAt = $null
            if ($quickDeaths -ge 3 -and -not $warnedCrash) {
                # once per companion session: the companion keeps working, the user can report it
                Show-Problem ("harvesting-dune stopped working and did not start again after 3 tries.`n`n" +
                    "The gaming.tools companion is not affected and works as usual.`n`n" +
                    "To get this fixed: in the folder $repo run scripts\doctor.ps1 -Report, then send the report " +
                    "file from the data folder, with data\logs\errors.log and data-run.prev.log, to the developer.")
                Write-Log 'detector keeps failing: problem message shown'
                $warnedCrash = $true
            }
        }
        if (-not $detector -and (Get-Date) -ge $nextStart) {
            if (-not (Test-Path -LiteralPath $detectorExe)) {
                if (-not $warnedExe) {
                    Write-Log "detector program missing ($detectorExe): run scripts\install.ps1 again"
                    $warnedExe = $true
                }
            } elseif (Test-Feed) {
                $warnedExe = $false
                $noFeedSince = $null
                $warnedNoFeed = $false
                $runLog = Join-Path $repo 'data-run.log'
                if (Test-Path -LiteralPath $runLog) {
                    Move-Item -LiteralPath $runLog -Destination (Join-Path $repo 'data-run.prev.log') -Force -ErrorAction SilentlyContinue
                }
                try {
                Start-Process -FilePath $detectorExe `
                    -ArgumentList 'run', '--devtools-port', $Port, '--http-port', $HttpPort, '--exit-when-companion-gone', 8 `
                    -WorkingDirectory $repo -WindowStyle Hidden `
                    -RedirectStandardError $runLog `
                    -RedirectStandardOutput (Join-Path $repo 'data-run.out') -ErrorAction Stop
                Write-Log 'companion feed ready: detector started'
                $startedAt = Get-Date
                } catch {
                    Write-Log "could not start the detector: $($_.Exception.Message) (run scripts\doctor.ps1)"
                    $nextStart = (Get-Date).AddSeconds(60)
                }
            } else {
                if (-not $noFeedSince) { $noFeedSince = Get-Date }
                if (-not $warnedNoFeed -and ((Get-Date) - $noFeedSince).TotalSeconds -gt 60) {
                    Write-Log "companion running but no DevTools feed on port $Port after 60 s: close it (tray icon > Exit) and open it again; scripts\doctor.ps1 checks why"
                    $warnedNoFeed = $true
                }
            }
        }
        if ($browserPending -and $detector) {
            $st = Get-SettingsState
            if ($st) {
                Start-Sleep -Seconds 3  # an already open tab reconnects within a second or two
                $st = Get-SettingsState
                if ($st -and $st.viewers -gt 0) {
                    Write-Log 'settings page already open in the browser: not opening another'
                } else {
                    Start-Process "http://127.0.0.1:$HttpPort/"
                    Write-Log 'settings page opened in the default browser'
                }
                $browserPending = $false
            }
        }
    } else {
        $browserPending = -not $NoBrowser  # open it again next time the companion starts
        $noFeedSince = $null
        $warnedNoFeed = $false
        $startedAt = $null
        $quickDeaths = 0
        $warnedCrash = $false
        if ($detector) {
            if (-not $goneSince) { $goneSince = Get-Date }
            if (((Get-Date) - $goneSince).TotalSeconds -gt 30) {
                Stop-Detector
                Write-Log 'companion closed: detector did not exit by itself, force-stopped'
                $goneSince = $null
            }
        } elseif ($goneSince) {
            Write-Log 'companion closed: detector saved and exited'
            $goneSince = $null
        }
    }
  } catch {
    Write-Log ("watcher error: {0} (line {1})" -f $_.Exception.Message, $_.InvocationInfo.ScriptLineNumber)
    Start-Sleep -Seconds 5
  }
    Start-Sleep -Seconds $PollSeconds
}
