<#
.SYNOPSIS
  Starts the gaming.tools companion with a localhost-only WebView2 DevTools port so
  harvesting-dune can read the companion's own map topics.

.DESCRIPTION
  The DevTools flag is passed through WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS in this
  process only: nothing is written to the registry or to user/system environment, and a
  normal companion launch afterwards has no debug port. The game is never touched.

  Security note: while the companion runs this way, any program on this PC can drive
  its web views through 127.0.0.1:<Port>. Relaunch it normally to close the port.

.PARAMETER Restart
  Close a running companion first (it only hides the minimap for a few seconds; the
  game keeps running).
#>
param(
    [int]$Port = 9231,
    [switch]$Restart,
    [string]$Exe  # default: found from the running companion or its installer entry
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'companion-path.ps1')
if (-not $Exe) { $Exe = Find-Companion }
if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) {
    throw 'gaming.tools companion not found. Install it, or pass -Exe <path to GT.Launcher.exe>.'
}

$running = Get-Process -Name 'GT.Launcher' -ErrorAction SilentlyContinue
if ($running) {
    if (-not $Restart) {
        throw 'The companion is already running. Close it (tray icon > Exit) or rerun with -Restart.'
    }
    foreach ($p in $running) { [void]$p.CloseMainWindow() }
    Wait-Process -InputObject $running -Timeout 10 -ErrorAction SilentlyContinue
    $still = Get-Process -Name 'GT.Launcher' -ErrorAction SilentlyContinue
    if ($still) { $still | Stop-Process -Force; Start-Sleep -Milliseconds 800 }
}

$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=$Port"
Start-Process -FilePath $Exe -WorkingDirectory (Split-Path -Parent $Exe)

$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 700
    try {
        $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/list" -TimeoutSec 2
        $pages = @($targets | Where-Object { $_.type -eq 'page' -and $_.url -like '*gaming.tools*' })
        if ($pages.Count -gt 0) {
            Write-Host "Companion feed ready on 127.0.0.1:$Port ($($pages.Count) page(s))."
            $pages | ForEach-Object { Write-Host "  $($_.url)" }
            exit 0
        }
    } catch { }
} while ((Get-Date) -lt $deadline)
throw "Companion started but no DevTools endpoint on port $Port after 30 s."
