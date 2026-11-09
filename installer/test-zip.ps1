<#
.SYNOPSIS
  End-to-end test of a release zip, as a guildmate uses it: extract it into a folder with a space,
  run Install.cmd without questions and without autorun, check the detector starts, run it again
  (an update must work in place), then Uninstall.cmd. The data folder must survive the uninstall.
  -UvSources and -UvDir go to install.ps1 (a local test must not use or change the PC's own uv).
#>
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [Parameter(Mandatory = $true)][string]$WorkDir,
    [string]$UvSources = '',
    [string]$UvDir = '',
    [string]$SyncServer = '',
    [string]$CaFile = ''
)
$ErrorActionPreference = 'Stop'
# the installer needs a guild key (it reads HARVESTING_DUNE_KEY, so the key stays off the command line)
if (-not $env:HARVESTING_DUNE_KEY) { throw 'Set HARVESTING_DUNE_KEY to a guild key first. The installer checks it with the server.' }
$extracted = Join-Path $WorkDir 'extracted zip'
$target = Join-Path $WorkDir 'install dir'
if (Test-Path -LiteralPath $WorkDir) { Remove-Item -LiteralPath $WorkDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $extracted | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path -LiteralPath $Zip).Path, $extracted)

function Invoke-Cmd([string]$cmdFile, [string[]]$arguments) {
    $quoted = @($arguments | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
    $p = Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList "/d /s /c `"`"$cmdFile`" $quoted`"" -NoNewWindow -Wait -PassThru
    return $p.ExitCode
}
function Assert([bool]$ok, [string]$what) {
    if (-not $ok) { throw "FAILED: $what" }
    Write-Host "OK: $what" -ForegroundColor Green
}

$common = @('-NonInteractive', '-NoAutorun', '-InstallDir', $target)
if ($UvSources) { $common += @('-UvSources', $UvSources) }
if ($UvDir) { $common += @('-UvDir', $UvDir) }
if ($SyncServer) { $common += @('-SyncServer', $SyncServer) }
if ($CaFile) { $common += @('-CaFile', $CaFile) }
$exe = Join-Path $target '.venv\Scripts\harvesting-dune.exe'
foreach ($run in 'first install', 'second install (idempotent update)') {
    $code = Invoke-Cmd (Join-Path $extracted 'Install.cmd') $common
    Assert ($code -eq 0) "$run exit code 0 (got $code)"
    Assert (Test-Path -LiteralPath (Join-Path $target '.harvesting-dune-install')) "$run wrote the install marker"
    & $exe --help | Out-Null
    Assert ($LASTEXITCODE -eq 0) "$run harvesting-dune.exe --help works"
    Assert (Test-Path -LiteralPath (Join-Path $target 'data\install.log')) "$run kept data\install.log"
}
Set-Content -LiteralPath (Join-Path $target 'data\keep-me.txt') -Value 'user data'
$code = Invoke-Cmd (Join-Path $extracted 'Uninstall.cmd') @('-NonInteractive', '-InstallDir', $target)
Assert ($code -eq 0) "uninstall exit code 0 (got $code)"
Assert (-not (Test-Path -LiteralPath (Join-Path $target '.venv'))) 'uninstall removed .venv'
Assert (-not (Test-Path -LiteralPath (Join-Path $target 'src'))) 'uninstall removed src'
Assert (Test-Path -LiteralPath (Join-Path $target 'data\keep-me.txt')) 'uninstall kept the data folder'
Write-Host 'zip install test passed' -ForegroundColor Green
