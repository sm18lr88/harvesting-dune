<#
.SYNOPSIS
  Updates an installed harvesting-dune by itself from the newest GitHub release: it looks for a new
  release while the game runs, and installs it when the game has closed.

.DESCRIPTION
  The autorun watcher (companion-autorun.ps1) starts this script hidden: when the game starts, every
  15 min while it runs, when it closes, and once after logon. Nothing happens in a development copy
  (a folder with .git: it updates with git).
  While the game runs (never more often than every $CheckMinutes):
    1. Downloads latest.json of the newest release (its version and the SHA256 of its zip).
    2. A newer version: downloads its zip, checks the SHA256, and unpacks it into the stage folder.
       The zip address comes from the checked version number, never from text in the download.
  While the game is closed:
    3. Installs an unpacked newer version: its own install.ps1 runs with -NonInteractive. It stops the
       detector and the watcher, keeps data\, and starts the watcher again.
    4. A failed install puts the earlier version back from a copy; the new version is tried again
       after $RetryHours.
  Log: data\update.log.
#>
param(
    [string]$ManifestUrl = 'https://github.com/sm18lr88/harvesting-dune/releases/latest/download/latest.json',
    [string]$StageDir = '',
    [int]$Port = 9231,
    [int]$CheckMinutes = 10,
    [int]$RetryHours = 24
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'app-files.ps1')
. (Join-Path $PSScriptRoot 'uv-setup.ps1')

$repo = Split-Path -Parent $PSScriptRoot
if (-not $StageDir) { $StageDir = $UpdateStageDir }
$logFile = Join-Path $repo 'data\update.log'

function Write-Log([string]$msg) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $msg
    try { Add-Content -LiteralPath $logFile -Value $line -Encoding utf8 -ErrorAction Stop } catch { }
}

function Get-Stamp([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return [datetime]::MinValue }
    try { return [datetime]::Parse((Get-Content -LiteralPath $path -TotalCount 1), $null, 'RoundtripKind') }
    catch { return [datetime]::MinValue }
}

function Set-Stamp([string]$path) { Set-Content -LiteralPath $path -Value (Get-Date).ToString('o') -Encoding ASCII }

function ConvertTo-VersionKey([string]$text) {
    # (numbers, is a release): 1.2.3-dev.7 is a build before release 1.2.3
    if ($text -notmatch '^(\d+)\.(\d+)\.(\d+)(-\S+)?$') { return $null }
    return @([version]"$($Matches[1]).$($Matches[2]).$($Matches[3])", (-not $Matches[4]))
}

function Test-Newer([string]$remote, [string]$local) {
    $r = ConvertTo-VersionKey $remote
    $l = ConvertTo-VersionKey $local
    if (-not $r -or -not $r[1] -or -not $l) { return $false }
    if ($r[0] -ne $l[0]) { return $r[0] -gt $l[0] }
    return (-not $l[1])
}

function Read-Manifest([string]$path) {
    $m = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ("$($m.version)" -notmatch '^\d+\.\d+\.\d+$' -or "$($m.sha256)" -notmatch '^[0-9a-fA-F]{64}$') {
        throw 'latest.json of the newest release has no valid version and SHA256'
    }
    return [pscustomobject]@{ Version = "$($m.version)"; Sha256 = "$($m.sha256)".ToLowerInvariant() }
}

function Save-Package([string]$version, [string]$sha256) {
    # unpacks the release into <stage>\<version>\pkg; the file 'ready' marks a checked, complete package
    $dir = Join-Path $StageDir $version
    if (Test-Path -LiteralPath (Join-Path $dir 'ready')) { return }
    if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $name = "harvesting-dune-$version.zip"
    $zip = Join-Path $dir $name
    Save-Download ($ManifestUrl.Substring(0, $ManifestUrl.LastIndexOf('/') + 1) + $name) $zip
    $hash = Get-Sha256 $zip
    if ($hash -ne $sha256) { throw "the zip of $version is damaged or changed (SHA256 $hash). It was not used." }
    $pkg = Join-Path $dir 'pkg'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $pkg)
    if ((Get-AppVersion $pkg) -ne $version -or -not (Test-Path -LiteralPath (Join-Path $pkg 'scripts\install.ps1'))) {
        throw "the zip of $version does not hold version $version of the program"
    }
    Remove-Item -LiteralPath $zip -Force
    Set-Stamp (Join-Path $dir 'ready')
    Write-Log "downloaded ${version}: it installs when the game closes"
}

function Get-Wanted {
    # the newest release when it is newer than this copy and did not fail in the last $RetryHours, else nothing
    $manifest = Join-Path $StageDir 'latest.json'
    if (-not (Test-Path -LiteralPath $manifest)) { return }
    $latest = Read-Manifest $manifest
    if (-not (Test-Newer $latest.Version (Get-AppVersion $repo))) {
        Get-ChildItem -LiteralPath $StageDir -Directory | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        return
    }
    if (((Get-Date) - (Get-Stamp (Join-Path $StageDir "failed-$($latest.Version)"))).TotalHours -lt $RetryHours) { return }
    return $latest
}

function Invoke-Install([string]$from, [string]$log) {
    # the installer of $from, hidden; it stops this folder's watcher and detector, then starts the watcher again
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -InstallDir "{1}" -NonInteractive -InstallUv -Port {2} -LogFile "{3}"' -f
        (Join-Path $from 'scripts\install.ps1'), $repo, $Port, $log
    $p = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru
    $null = $p.Handle  # Windows PowerShell 5.1 loses the exit code without this
    if (-not $p.WaitForExit(30 * 60 * 1000)) {
        & taskkill.exe /PID $p.Id /T /F *> $null
        return -1
    }
    return $p.ExitCode
}

function Install-Package([string]$version) {
    $local = Get-AppVersion $repo
    $previous = Join-Path $StageDir 'previous'
    Copy-AppFiles $repo $previous  # the running version, to put back if the new one fails
    New-Item -ItemType Directory -Force -Path (Join-Path $repo 'data') | Out-Null
    # the restarted watcher reads this: no new browser tab for an update
    Set-Content -LiteralPath (Join-Path $repo 'data\update-restart') -Value $version -Encoding ASCII
    Write-Log "installing $version (was $local)"
    $code = Invoke-Install (Join-Path $StageDir "$version\pkg") (Join-Path $StageDir "install-$version.log")
    if ($code -eq 0) {
        Write-Log "updated to $version"
        Remove-Item -LiteralPath (Join-Path $StageDir $version), $previous -Recurse -Force -ErrorAction SilentlyContinue
        return
    }
    Set-Stamp (Join-Path $StageDir "failed-$version")
    Write-Log "install of $version failed (exit $code, see $StageDir\install-$version.log): putting $local back"
    Set-Content -LiteralPath (Join-Path $repo 'data\update-restart') -Value "$local (put back: $version failed)" -Encoding ASCII
    $code = Invoke-Install $previous (Join-Path $StageDir "restore-$local.log")
    if ($code -eq 0) { Write-Log "$local is back" } else { Write-Log "putting $local back failed (exit $code): run Install.cmd again" }
}

function Invoke-Update {
    if (Test-Path -LiteralPath (Join-Path $repo '.git')) { return }  # a development copy updates with git
    New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
    if (Get-Process -Name $GameProcess -ErrorAction SilentlyContinue) {
        # playing: look and download, never install
        $checked = Join-Path $StageDir 'checked'
        if (((Get-Date) - (Get-Stamp $checked)).TotalMinutes -ge $CheckMinutes) {
            Set-Stamp $checked  # also after a failed check: an offline PC does not retry at every start
            $manifest = Join-Path $StageDir 'latest.json'
            Save-Download $ManifestUrl "$manifest.new"
            Move-Item -LiteralPath "$manifest.new" -Destination $manifest -Force
        }
        $wanted = Get-Wanted
        if ($wanted) { Save-Package $wanted.Version $wanted.Sha256 }
        return
    }
    $wanted = Get-Wanted
    if ($wanted -and (Test-Path -LiteralPath (Join-Path $StageDir "$($wanted.Version)\ready"))) { Install-Package $wanted.Version }
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\harvesting-dune-update')
if (-not $mutex.WaitOne(0)) { exit 0 }  # an update is already running
if ((Test-Path -LiteralPath $logFile) -and (Get-Item -LiteralPath $logFile).Length -gt 1MB) { Remove-Item -LiteralPath $logFile -Force }
try {
    Invoke-Update
} catch {
    Write-Log "update stopped: $($_.Exception.Message)"
    exit 1
}
