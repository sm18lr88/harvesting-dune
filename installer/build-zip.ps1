<#
.SYNOPSIS
  Builds dist\harvesting-dune-<version>.zip: the release zip for guildmates (Install.cmd at the root).
  It holds only what users run: the files listed in scripts\app-files.ps1, plus version.txt.
  Usage: powershell -ExecutionPolicy Bypass -File installer\build-zip.ps1 -Version 1.2.3
#>
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$OutDir = ''
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $repo 'scripts\app-files.ps1')
if (-not $OutDir) { $OutDir = Join-Path $repo 'dist' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$zipPath = Join-Path (Get-FullPath $OutDir) "harvesting-dune-$Version.zip"

$entries = New-Object System.Collections.Generic.List[object]  # @(full path, name in the zip)
foreach ($d in $AppDirs) {
    $dir = Join-Path $repo $d
    if (-not (Test-Path -LiteralPath $dir)) { continue }
    Get-ChildItem -LiteralPath $dir -Recurse -File |
        Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' } |
        ForEach-Object { $entries.Add(@($_.FullName, ($d + $_.FullName.Substring($dir.Length)).Replace('\', '/'))) }
}
foreach ($f in $UserScripts) {
    $path = Join-Path $repo "scripts\$f"
    if (-not (Test-Path -LiteralPath $path)) { throw "scripts\$f is missing" }
    $entries.Add(@($path, "scripts/$f"))
}
foreach ($f in @($AppFiles | Where-Object { $_ -ne 'version.txt' })) {
    $path = Join-Path $repo $f
    if (Test-Path -LiteralPath $path) { $entries.Add(@($path, $f)) }
    elseif ($f -in 'Install.cmd', 'Uninstall.cmd', 'pyproject.toml', 'uv.lock') { throw "$f is missing" }
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
$zip = [IO.Compression.ZipFile]::Open($zipPath, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($e in $entries) {
        if ($e[1] -like '*.cmd') {
            # cmd.exe misreads labels in LF-only batch files: always ship CRLF
            $text = [IO.File]::ReadAllText($e[0]) -replace "`r?`n", "`r`n"
            $w = New-Object IO.StreamWriter(($zip.CreateEntry($e[1])).Open(), (New-Object Text.ASCIIEncoding))
            try { $w.Write($text) } finally { $w.Dispose() }
        } else {
            [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $e[0], $e[1])
        }
    }
    $w = New-Object IO.StreamWriter(($zip.CreateEntry('version.txt')).Open(), (New-Object Text.ASCIIEncoding))
    try { $w.Write("$Version`r`n") } finally { $w.Dispose() }
} finally {
    $zip.Dispose()
}
Write-Host ("Built {0} ({1} files, {2:N0} KB)" -f $zipPath, ($entries.Count + 1), ((Get-Item -LiteralPath $zipPath).Length / 1KB))
