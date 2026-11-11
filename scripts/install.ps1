<#
.SYNOPSIS
  One-step setup of harvesting-dune for the gaming.tools Dune companion.

.DESCRIPTION
  1. Checks this PC, then (with -FromPackage or -InstallDir) copies the program into the install
     folder, %LOCALAPPDATA%\Programs\harvesting-dune by default. A newer copy updates in place and
     keeps the data folder.
  2. Finds the installed gaming.tools companion.
  3. Finds uv, or gets it (-InstallUv): winget, then Astral's installer, then the pinned uv zip
     from GitHub (SHA256 checked). uv gets its own Python; a system Python is never used.
  4. Installs the detector's Python environment with uv.
  5. Saves the guild key. The key is required: hidden input, or -GuildKey / HARVESTING_DUNE_KEY.
  6. Makes every companion launch expose a localhost-only DevTools port, and registers a hidden
     logon task that starts the detector with the companion (see install-autorun.ps1).

  Users double-click Install.cmd (release zip). From a clone:
      powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -InstallUv

  Every step reports what it did. A failure says why and how to fix it. The output is kept in
  data\install.log. scripts\doctor.ps1 checks a finished setup. No administrator rights needed.

.PARAMETER InstallUv          If uv is missing, get it (see -UvSources).
.PARAMETER RestartCompanion   Restart a running companion now, so its DevTools port opens.
.PARAMETER NoAutorun          Install only. No autorun, and no other copy of the detector is stopped.
.PARAMETER Uninstall          Stop the detector, remove autorun and (in an install folder) the program files. data\ stays.
.PARAMETER FromPackage        Run from an extracted release zip: install into -InstallDir (default per-user folder).
.PARAMETER InstallDir         Install into this folder instead of running in place.
.PARAMETER NonInteractive     Ask nothing. Needs a guild key saved before, or -GuildKey / HARVESTING_DUNE_KEY.
.PARAMETER LogFile            Write the install log here (it is also copied to data\install.log).
.PARAMETER UvSources          Where to look for uv, in order: existing, winget, astral, github.
.PARAMETER UvDir              Where a downloaded uv goes (default %USERPROFILE%\.local\bin, as Astral's installer).
.PARAMETER UvDownloadUrl      Another URL for the uv zip (tests); the SHA256 check still applies.
.PARAMETER GuildKey           The guild key to save, without questions (default: env HARVESTING_DUNE_KEY).
.PARAMETER SyncServer         The server that checks the guild key (tests use a local server).
.PARAMETER CaFile             The CA certificate for SyncServer (default: sync-ca.pem of the program).
#>
param(
    [switch]$InstallUv,
    [switch]$RestartCompanion,
    [switch]$Uninstall,
    [switch]$NoAutorun,
    [switch]$FromPackage,
    [string]$InstallDir = '',
    [switch]$NonInteractive,
    [string]$LogFile = '',
    [string[]]$UvSources = @('existing', 'winget', 'astral', 'github'),
    [string]$UvDir = '',
    [string]$UvDownloadUrl = '',
    [int]$Port = 9231,
    [string]$GuildKey = $env:HARVESTING_DUNE_KEY,
    [string]$SyncServer = 'https://158.69.179.64:8443',
    [string]$CaFile = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'companion-path.ps1')
. (Join-Path $PSScriptRoot 'uv-setup.ps1')
. (Join-Path $PSScriptRoot 'app-files.ps1')
. (Join-Path $PSScriptRoot 'guild-key.ps1')

$src = Split-Path -Parent $PSScriptRoot
# Uninstall.cmd always passes -FromPackage: run from an installed copy (it holds the marker), that copy is the target
if ($FromPackage -and -not $InstallDir) {
    $InstallDir = if (Test-Path -LiteralPath (Join-Path $src $AppMarker)) { $src } else { Join-Path $env:LOCALAPPDATA 'Programs\harvesting-dune' }
}
$repo = if ($InstallDir) { Get-FullPath $InstallDir } else { Get-FullPath $src }
if (-not $CaFile) { $CaFile = Join-Path $repo 'src\harvesting_dune\sync-ca.pem' }
$copyApp = -not (Test-SamePath $src $repo)
$UvSources = @($UvSources | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
if (-not $UvDir) { $UvDir = Join-Path $env:USERPROFILE '.local\bin' }
$taskName = $AutorunTaskName
$doctorCmd = "powershell -ExecutionPolicy Bypass -File `"$repo\scripts\doctor.ps1`""
$script:stoppedTask = $false

function Stop-Running([switch]$ThisFolderOnly) {
    # -ThisFolderOnly: never stop another copy of harvesting-dune (a development folder, or a test)
    if (-not $ThisFolderOnly -and (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $script:stoppedTask = $true
    }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match 'companion-autorun\.ps1' -or $_.CommandLine -match 'harvesting-dune(\.exe)?"?\s+run') } |
        Where-Object { -not $ThisFolderOnly -or $_.CommandLine.IndexOf("$repo\", [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }  # may already be gone with its parent
}

$script:step = 0
function Step([string]$what) {
    $script:step++
    Write-Host ("[{0}/8] {1}" -f $script:step, $what) -ForegroundColor Cyan
}

function Close-Log {
    try { Stop-Transcript | Out-Null } catch { }
    $copy = Join-Path $repo 'data\install.log'
    if ($LogFile -and -not $Uninstall -and (Test-Path -LiteralPath $repo)) {
        New-Item -ItemType Directory -Force -Path (Join-Path $repo 'data') -ErrorAction SilentlyContinue | Out-Null
        Copy-Item -LiteralPath $logFile -Destination $copy -Force -ErrorAction SilentlyContinue
    }
}

function Fail([string]$reason, [string]$fix) {
    Write-Host ''
    Write-Host "Install failed: $reason" -ForegroundColor Red
    if ($fix) { Write-Host "How to fix: $fix" -ForegroundColor Yellow }
    if (Test-Path -LiteralPath (Join-Path $repo 'scripts\doctor.ps1')) { Write-Host "Full check of your setup: $doctorCmd" -ForegroundColor Yellow }
    Write-Host "Install log: $logFile"
    if ($script:stoppedTask) { Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }  # the earlier install keeps working
    Close-Log
    exit 1
}

# an uninstall never creates the folder it removes: a mistyped -InstallDir must stay "not installed"
$logFile = if ($LogFile) { $LogFile } elseif ($Uninstall) { Join-Path $env:TEMP 'harvesting-dune-uninstall.log' } else { Join-Path $repo 'data\install.log' }
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFile) | Out-Null
    Start-Transcript -Path $logFile -Force | Out-Null
} catch {
    Write-Host "Cannot write the install log ($logFile): $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'How to fix: make sure the disk is not full and that this is your own Windows account. Then run the installer again.' -ForegroundColor Yellow
    exit 1
}

if ($Uninstall) {
    if (-not (Test-Path -LiteralPath $repo)) {
        Write-Host "harvesting-dune is not installed in $repo. Nothing to do."
        Close-Log; exit 0
    }
    if (Test-TaskIsOurs $repo) { Stop-Running } else { Stop-Running -ThisFolderOnly }  # another copy's autorun stays
    $autorun = Join-Path $repo 'scripts\install-autorun.ps1'
    if (Test-Path -LiteralPath $autorun) { & $autorun -Uninstall }
    if (Test-Path -LiteralPath (Join-Path $repo $AppMarker)) {
        Start-Sleep -Milliseconds 500
        $left = @(Remove-AppFiles $repo -KeepUninstaller:(-not $copyApp))
        Remove-Item -LiteralPath $UpdateStageDir -Recurse -Force -ErrorAction SilentlyContinue  # downloaded updates
        Write-Host 'harvesting-dune is uninstalled. Restart the companion to close its DevTools port.' -ForegroundColor Green
        Write-Host "Your learned data stays in $repo\data. To remove everything, delete the folder $repo."
        if ($left.Count) { Write-Host "Windows did not let the uninstaller remove: $($left -join ', '). Delete them by hand after a restart." -ForegroundColor Yellow }
    } else {
        Write-Host 'harvesting-dune is uninstalled. Restart the companion to close its DevTools port.'
        Write-Host "You can now delete $repo (your learned data is in its data\ folder)."
    }
    try { Stop-Transcript | Out-Null } catch { }
    exit 0
}

Step 'Checking this PC'
if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or [Environment]::OSVersion.Version.Major -lt 10) {
    Fail 'harvesting-dune needs Windows 10 or 11.' ''
}
if ($PSVersionTable.PSVersion.Major -lt 5) { Fail "Windows PowerShell $($PSVersionTable.PSVersion) is too old." 'Install the Windows updates. Windows 10 and 11 include PowerShell 5.1.' }
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    Fail 'PowerShell runs in restricted mode here (set by an administrator or security software).' 'Ask the PC administrator to allow PowerShell scripts.'
}
if ($copyApp -and (Test-TempPath $src)) {
    Fail "the installer runs from a temporary folder ($src). Windows does this when you open the ZIP file without extracting it." 'Right-click the ZIP file, select "Extract All", open the new folder, and double-click Install.cmd there.'
}
if (Test-TempPath $repo) {
    Fail "the install folder is temporary ($repo)." 'Extract the ZIP file to a permanent folder, for example Documents\harvesting-dune. Then run the installer there. The autorun task always uses this folder.'
}
if ($copyApp -and ((Test-Inside $src $repo) -or (Test-Inside $repo $src))) {
    Fail "the install folder ($repo) and the extracted ZIP folder ($src) are inside each other." 'Extract the ZIP file to another folder, for example Downloads. Then run Install.cmd again.'
}
if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    Fail 'the Windows Task Scheduler commands are unavailable.' 'Run the installer in Windows PowerShell (powershell.exe), not a restricted or 32-bit shell.'
}
foreach ($place in @($repo, $env:LOCALAPPDATA)) {
    $free = Get-FreeMB $place
    if ($null -ne $free -and $free -lt 500) {
        Fail "the disk of $place has only $free MB free. The install needs about 500 MB." 'Free some disk space (empty the Recycle Bin, remove old downloads). Then run the installer again.'
    }
}
if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning 'The installer runs as administrator. This is not necessary. If you play with another Windows account, run it again from that account.'
}
# files from a downloaded ZIP carry the "from the internet" mark; clear it for our own files
Get-ChildItem -LiteralPath $src -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

Step $(if ($copyApp) { "Copying the program to $repo" } else { "Using the program in $repo" })
# a running detector locks its files in .venv, and an old watcher keeps its old script
if ($NoAutorun) { Stop-Running -ThisFolderOnly } else { Stop-Running }
Start-Sleep -Milliseconds 500
if ($copyApp) {
    $old = if (Test-Path -LiteralPath (Join-Path $repo $AppMarker)) { Get-AppVersion $repo }
    if ($old) { Write-Host "      updating $old -> $(Get-AppVersion $src). Your data folder stays." } else { Write-Host "      version $(Get-AppVersion $src)" }
    try { Copy-AppFiles $src $repo } catch {
        $msg = $_.Exception.Message
        if ($msg -match 'not enough space|disk is full') { Fail "the disk is full ($msg)." 'Free some disk space. Then run the installer again.' }
        Fail "the program files were not copied: $msg" "Close the companion and any window that shows a file of $repo. If an antivirus blocked the copy, allow that folder. Then run the installer again."
    }
}

Step 'Looking for the gaming.tools companion'
$companion = Find-Companion
if ($companion) {
    Write-Host "      found: $companion"
} else {
    Write-Warning 'The gaming.tools companion is not installed. Install it from https://gaming.tools. The setup continues. The detector starts when the companion runs.'
}

Step 'Checking uv (Python package manager)'
$uv = Find-Uv -UvDir $UvDir -SkipExisting:($UvSources -notcontains 'existing')
if (-not $uv -and $InstallUv) { $uv = Install-Uv $UvSources $UvDir $UvDownloadUrl }
if (-not $uv) {
    foreach ($p in $script:UvProblems) { Write-Host "      $p" -ForegroundColor Yellow }
    if (-not $InstallUv) { Fail 'uv is required.' 'Run Install.cmd, or run this script again with -InstallUv.' }
    Fail 'uv (the Python package manager) could not be installed. The reasons are above.' 'Check that the internet works. If you use a VPN or a proxy, try without it. Or install uv yourself from https://docs.astral.sh/uv/getting-started/installation/ . Then run the installer again.'
}
Write-Host "      $uv"

Step 'Installing the detector (uv sync; downloads Python the first time)'
# settings of the user's own uv work could break this install; uv refuses two Python preferences
Remove-Item Env:UV_PROJECT_ENVIRONMENT, Env:UV_OFFLINE, Env:VIRTUAL_ENV, Env:UV_MANAGED_PYTHON, Env:UV_NO_MANAGED_PYTHON -ErrorAction SilentlyContinue
$env:UV_PYTHON_PREFERENCE = 'only-managed'  # uv's own Python (3.14, as tested), never a system Python
$env:UV_PYTHON_DOWNLOADS = 'automatic'
$env:NO_COLOR = '1'
Set-UvProxy
for ($attempt = 1; ; $attempt++) {
    $lines = New-Object System.Collections.Generic.List[string]
    Push-Location -LiteralPath $repo
    try {
        $ErrorActionPreference = 'Continue'  # uv reports progress on stderr
        & $uv sync --no-dev --python 3.14 2>&1 | ForEach-Object { $line = "$_"; $lines.Add($line); Write-Host "      $line" }
        $code = $LASTEXITCODE
    } catch {
        $code = -1
        $lines.Add($_.Exception.Message)
    } finally {
        $ErrorActionPreference = 'Stop'
        Pop-Location
    }
    if ($code -eq 0) { break }
    $text = $lines -join "`n"
    $tls = $text -match 'certificate|UnknownIssuer|invalid peer|tls'
    $net = $text -match 'dns error|failed to lookup|error sending request|connect|timed out|Connection reset|network'
    if ($attempt -lt 3 -and ($tls -or $net)) {
        if ($tls) { $env:UV_NATIVE_TLS = '1'; Write-Host '      retrying with the Windows certificate store...' } else { Write-Host '      retrying in 5 s...' }
        Start-Sleep -Seconds 5
        continue
    }
    if ($text -match 'os error 112|not enough space|No space left') { Fail 'the disk is full.' 'Free some disk space. Then run the installer again.' }
    if ($text -match 'os error 5\b|os error 32\b|Access is denied|used by another process') {
        Fail 'a file is locked or blocked (uv sync failed).' "Close the companion. If your antivirus quarantined a file, allow the folders $repo and $env:APPDATA\uv. Then run the installer again."
    }
    if ($tls) { Fail 'the secure (HTTPS) connection was refused (uv sync failed).' 'A proxy, firewall or antivirus that checks HTTPS traffic can cause this. Try another network, or turn off HTTPS scanning in the antivirus. Check that the PC clock is correct.' }
    if ($net) { Fail 'the download of Python or a package failed: no internet connection (uv sync failed).' 'Check that the internet works. If you use a VPN or a proxy, try without it. Then run the installer again.' }
    Fail "uv sync failed (exit $code). Its message is above." "Delete the folder $repo\.venv and run the installer again."
}
$exe = Join-Path $repo '.venv\Scripts\harvesting-dune.exe'
if (-not (Test-Path -LiteralPath $exe)) { Fail "uv sync finished but $exe is missing." "Delete the folder $repo\.venv and run the installer again. If it fails again, an antivirus possibly removed the file: allow that folder." }
$ErrorActionPreference = 'Continue'
$null = & $exe --help 2>&1
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($code -ne 0) { Fail 'the detector was installed but does not start.' "Delete the folder $repo\.venv and run the installer again." }

Step 'Guild key'
$keyProblem = Invoke-GuildKeyStep $repo $GuildKey (-not ($NonInteractive -or [Console]::IsInputRedirected)) $SyncServer $CaFile
if ($keyProblem) { Fail $keyProblem[0] $keyProblem[1] }

Step 'Registering autorun (companion DevTools setting + logon task)'
if ($NoAutorun) {
    Write-Host '      skipped (-NoAutorun). The detector does not start with the companion.'
    Remove-OwnAutorun $repo
} else {
    $machinePolicy = (Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments' -ErrorAction SilentlyContinue).'GT.Launcher.exe'
    if ($machinePolicy -and $machinePolicy -ne "--remote-debugging-port=$Port") {
        Write-Warning "A machine-wide WebView2 policy for the companion ('$machinePolicy') replaces the setting of this installer. Ask the PC administrator to remove HKLM\SOFTWARE\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments\GT.Launcher.exe."
    }
    try {
        & (Join-Path $repo 'scripts\install-autorun.ps1') -Port $Port
        Start-ScheduledTask -TaskName $taskName
        $script:stoppedTask = $false
    } catch {
        Fail "the installer did not register the autorun task: $($_.Exception.Message)" 'Run the installer as the Windows user who plays. Administrator rights are not necessary. If it fails again, a group policy possibly blocks scheduled tasks.'
    }
}

Step 'Checking the companion'
$running = Get-Process -Name 'GT.Launcher' -ErrorAction SilentlyContinue
$portOpen = $false
if ($running) {
    try { $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2 -UseBasicParsing; $portOpen = $true } catch { }
}
if ($NoAutorun) {
    Write-Host '      autorun is off: nothing to start.'
} elseif ($portOpen) {
    Write-Host '      the companion is running with its DevTools port: the detector is starting now.'
} elseif ($running -and $RestartCompanion -and $companion) {
    Write-Host '      restarting the companion so its DevTools port opens...'
    foreach ($p in $running) { [void]$p.CloseMainWindow() }
    Wait-Process -InputObject $running -Timeout 10 -ErrorAction SilentlyContinue
    Get-Process -Name 'GT.Launcher' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
    Start-Process -FilePath $companion -WorkingDirectory (Split-Path -Parent $companion)
} elseif ($running) {
    Write-Host '      the companion was started before this install, so it has no DevTools port yet.' -ForegroundColor Yellow
    Write-Host '      Close it (tray icon > Exit) and open it again.' -ForegroundColor Yellow
} else {
    Write-Host '      the companion is not running. The detector starts when you open it.'
}

Write-Host ''
if ($NoAutorun) {
    Write-Host 'Done. Autorun is off: the detector does not start with the companion.' -ForegroundColor Green
    Write-Host "To start it by hand, run: `"$exe`" run" -ForegroundColor Green
    Write-Host 'To turn autorun on, run this installer again without -NoAutorun. If lines do not appear, run:' -ForegroundColor Green
} else {
    Write-Host 'Done. Opening the gaming.tools companion now starts harvesting-dune and its settings page' -ForegroundColor Green
    Write-Host '(http://127.0.0.1:8765). If lines do not appear, run:' -ForegroundColor Green
}
Write-Host "    $doctorCmd"
Write-Host "Install log: $logFile"
Close-Log
