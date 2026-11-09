<#
.SYNOPSIS
  Dot-source for the list of program files and for copying them into (or removing them from) the
  per-user install folder. The release zip (installer\build-zip.ps1) uses the same list.
  Only a folder with the install marker file is ever cleaned, so a development clone stays intact.
#>

$AppDirs = @('src', 'docs')
$AppFiles = @('pyproject.toml', 'uv.lock', 'README.md', 'LICENSE', 'version.txt', 'Install.cmd', 'Uninstall.cmd')
$UserScripts = @('install.ps1', 'install-autorun.ps1', 'companion-autorun.ps1', 'companion-path.ps1',
    'doctor.ps1', 'uv-setup.ps1', 'app-files.ps1', 'guild-key.ps1')
$AppMarker = '.harvesting-dune-install'

function Get-FullPath([string]$path) { return [IO.Path]::GetFullPath($path).TrimEnd('\') }

function Test-SamePath([string]$a, [string]$b) { return (Get-FullPath $a) -eq (Get-FullPath $b) }

function Test-Inside([string]$parent, [string]$child) {
    return ((Get-FullPath $child) + '\').StartsWith((Get-FullPath $parent) + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Test-TempPath([string]$path) {
    $full = (Get-FullPath $path) + '\'
    if ($full -match '\\AppData\\Local\\Temp\\' -or $full -match '\.zip\\') { return $true }
    foreach ($t in @($env:TEMP, $env:TMP)) {
        if ($t -and (Test-Inside $t $full)) { return $true }
    }
    return $false
}

function Get-AppVersion([string]$dir) {
    $file = Join-Path $dir 'version.txt'
    if (Test-Path -LiteralPath $file) { return (Get-Content -LiteralPath $file -TotalCount 1).Trim() }
    $toml = Join-Path $dir 'pyproject.toml'
    if (Test-Path -LiteralPath $toml) {
        $m = Select-String -LiteralPath $toml -Pattern '^version = "(.+)"' | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value }
    }
    return $null
}

function Get-FreeMB([string]$path) {
    try {
        $drive = New-Object System.IO.DriveInfo ([IO.Path]::GetPathRoot((Get-FullPath $path)))
        return [int]($drive.AvailableFreeSpace / 1MB)
    } catch { return $null }  # a network path: not measurable, not blocking
}

function Invoke-WithRetry([scriptblock]$action) {
    # an antivirus scan holds a fresh file for a moment; a second try usually works
    for ($i = 1; ; $i++) {
        try { & $action; return } catch {
            if ($i -ge 4) { throw }
            Start-Sleep -Seconds (2 * $i)
        }
    }
}

function Copy-AppFiles([string]$from, [string]$to) {
    New-Item -ItemType Directory -Force -Path $to | Out-Null
    foreach ($d in $AppDirs + @('scripts')) {
        $target = Join-Path $to $d
        if (Test-Path -LiteralPath $target) { Invoke-WithRetry { Remove-Item -LiteralPath $target -Recurse -Force } }
    }
    foreach ($d in $AppDirs) {
        $source = Join-Path $from $d
        if (Test-Path -LiteralPath $source) {
            Invoke-WithRetry { Copy-Item -LiteralPath $source -Destination (Join-Path $to $d) -Recurse -Force }
        }
    }
    Get-ChildItem -LiteralPath (Join-Path $to 'src') -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path (Join-Path $to 'scripts') | Out-Null
    foreach ($f in $UserScripts) {
        $source = Join-Path $from "scripts\$f"
        if (Test-Path -LiteralPath $source) { Invoke-WithRetry { Copy-Item -LiteralPath $source -Destination (Join-Path $to 'scripts') -Force } }
    }
    foreach ($f in $AppFiles) {
        $source = Join-Path $from $f
        if (Test-Path -LiteralPath $source) { Invoke-WithRetry { Copy-Item -LiteralPath $source -Destination $to -Force } }
    }
    Set-Content -LiteralPath (Join-Path $to $AppMarker) -Value ([string](Get-AppVersion $from)) -Encoding ASCII
}

function Remove-AppFiles([string]$dir, [switch]$KeepUninstaller) {
    # returns the names it could not remove; data\ always stays
    $left = @()
    foreach ($d in $AppDirs + @('scripts', '.venv')) {
        $path = Join-Path $dir $d
        if (Test-Path -LiteralPath $path) {
            try { Invoke-WithRetry { Remove-Item -LiteralPath $path -Recurse -Force } } catch { $left += $d }
        }
    }
    foreach ($f in $AppFiles + @($AppMarker, 'data-run.log', 'data-run.out', 'data-run.prev.log')) {
        if ($KeepUninstaller -and $f -eq 'Uninstall.cmd') { continue }
        $path = Join-Path $dir $f
        if (Test-Path -LiteralPath $path) {
            try { Invoke-WithRetry { Remove-Item -LiteralPath $path -Force } } catch { $left += $f }
        }
    }
    return $left
}
