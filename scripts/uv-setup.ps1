<#
.SYNOPSIS
  Dot-source to get Find-Uv (a working uv.exe that is already on this PC) and Install-Uv (gets
  one with winget, Astral's installer, or the pinned uv release zip from GitHub checked by SHA256).
  Install-Uv returns the full path of uv.exe: callers use that path, never PATH, because PATH
  changes reach only new windows.
#>

$UvVersion = '0.13.7'
$UvSha256 = @{  # from the .sha256 files of that GitHub release
    'x86_64'  = '0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021'
    'aarch64' = '505d9e65e7692bac3ddf98c66754aa26c5050c409c184af30a881a0a72cc85e3'
}
$script:UvProblems = New-Object System.Collections.Generic.List[string]

function Get-UvArch {
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    if ($arch -eq 'ARM64') { return 'aarch64' }
    return 'x86_64'
}

function Test-Uv([string]$path) {
    $ErrorActionPreference = 'Continue'
    if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    try { $null = & $path --version 2>&1; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

function Find-Uv([string]$UvDir, [switch]$SkipExisting) {
    $candidates = @()
    if ($UvDir) { $candidates += Join-Path $UvDir 'uv.exe' }
    if (-not $SkipExisting) {
        $candidates += @(Get-Command uv.exe -All -CommandType Application -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
        foreach ($dir in @($env:UV_INSTALL_DIR, $env:XDG_BIN_HOME,
                (Join-Path $env:USERPROFILE '.local\bin'), (Join-Path $env:LOCALAPPDATA 'Programs\uv'),
                (Join-Path $env:USERPROFILE '.cargo\bin'), (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'))) {
            if ($dir) { $candidates += Join-Path $dir 'uv.exe' }
        }
        $candidates += @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\astral-sh.uv_*\uv.exe') -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    }
    foreach ($c in $candidates) { if (Test-Uv $c) { return $c } }
    return $null
}

function Enable-ModernTls {
    # Windows PowerShell 5.1 on an older Windows 10 offers only TLS 1.0, which GitHub refuses
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
    try { [Net.WebRequest]::DefaultWebProxy.Credentials = [Net.CredentialCache]::DefaultNetworkCredentials } catch { }
}

function Get-NetProblem([string]$msg) {
    switch -Regex ($msg) {
        'could not be resolved|No such host|name or service not known' { return 'this PC cannot reach the internet (the server name was not found).' }
        '\(407\)|[Pp]roxy' { return 'the proxy server of this network needs a sign-in.' }
        'SSL|TLS|secure channel|trust relationship|[Cc]ertificate' { return 'the secure (HTTPS) connection was refused. A proxy, firewall or antivirus that checks HTTPS traffic, or a wrong PC clock, can cause this.' }
        'timed out|[Tt]imeout' { return 'the download took too long (the connection is slow or blocked).' }
        'not enough space|disk is full' { return 'the disk is full.' }
        '\(404\)|Not Found' { return 'the file is not on the server (404).' }
    }
    return "the download failed: $msg"
}

function Save-Download([string]$url, [string]$file) {
    Enable-ModernTls
    $last = ''
    for ($i = 1; $i -le 3; $i++) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing -TimeoutSec 120
            return
        } catch {
            $last = $_.Exception.Message
            Write-Host "      download try $i of 3 failed: $last"
            if ($i -lt 3) { Start-Sleep -Seconds (3 * $i) }
        }
    }
    throw (Get-NetProblem $last)
}

function Invoke-Logged([string]$exe, [string]$arguments, [int]$timeoutSec) {
    # its output goes into the install log; a hung tool is stopped instead of hanging the install
    $out = [IO.Path]::GetTempFileName()
    $err = [IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath $exe -ArgumentList $arguments -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
        $null = $p.Handle  # Windows PowerShell 5.1 loses the exit code without this
        if (-not $p.WaitForExit($timeoutSec * 1000)) {
            & taskkill.exe /PID $p.Id /T /F *> $null
            Write-Host "      stopped after $timeoutSec s without an answer"
            return -1
        }
        foreach ($f in $out, $err) {
            Get-Content -LiteralPath $f -ErrorAction SilentlyContinue | Where-Object { $_.Trim() } | ForEach-Object { Write-Host "      $_" }
        }
        return $p.ExitCode
    } finally {
        Remove-Item -LiteralPath $out, $err -Force -ErrorAction SilentlyContinue
    }
}

function Install-UvWinget {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { $script:UvProblems.Add('winget: not available on this PC.'); return }
    Write-Host '      trying winget...'
    $code = Invoke-Logged $winget.Source 'install --id astral-sh.uv --exact --source winget --accept-source-agreements --accept-package-agreements --silent --disable-interactivity' 300
    if ($code -ne 0) { $script:UvProblems.Add("winget: exit code $code.") }
}

function Install-UvAstral([string]$UvDir, [string]$tmp) {
    Write-Host "      trying Astral's installer (uv $UvVersion)..."
    $installer = Join-Path $tmp 'uv-installer.ps1'
    try { Save-Download "https://astral.sh/uv/$UvVersion/install.ps1" $installer } catch { $script:UvProblems.Add("Astral's installer: $($_.Exception.Message)"); return }
    $env:UV_UNMANAGED_INSTALL = $UvDir  # into this folder only: no PATH change, no install receipt
    try {
        $code = Invoke-Logged (Join-Path $PSHOME 'powershell.exe') "-NoProfile -ExecutionPolicy Bypass -File `"$installer`"" 300
        if ($code -ne 0) { $script:UvProblems.Add("Astral's installer: exit code $code.") }
    } finally {
        Remove-Item Env:UV_UNMANAGED_INSTALL -ErrorAction SilentlyContinue
    }
}

function Install-UvGitHub([string]$UvDir, [string]$tmp, [string]$url) {
    $arch = Get-UvArch
    if (-not $url) { $url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-$arch-pc-windows-msvc.zip" }
    Write-Host "      downloading uv $UvVersion from GitHub..."
    $zip = Join-Path $tmp 'uv.zip'
    try { Save-Download $url $zip } catch { $script:UvProblems.Add("GitHub download: $($_.Exception.Message)"); return }
    $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $UvSha256[$arch]) {
        $script:UvProblems.Add("GitHub download: the file is damaged or changed (SHA256 $hash). It was not used.")
        return
    }
    try {
        $unpacked = Join-Path $tmp 'uv'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $unpacked)
        New-Item -ItemType Directory -Force -Path $UvDir | Out-Null
        Get-ChildItem -LiteralPath $unpacked -Recurse -Filter 'uv*.exe' | Copy-Item -Destination $UvDir -Force
    } catch {
        $script:UvProblems.Add("GitHub download: could not unpack uv into $UvDir ($($_.Exception.Message)).")
    }
}

function Install-Uv([string[]]$Sources, [string]$UvDir, [string]$DownloadUrl) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('harvesting-dune-uv-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        foreach ($source in @($Sources | Where-Object { $_ -in 'winget', 'astral', 'github' })) {
            switch ($source) {
                'winget' { Install-UvWinget }
                'astral' { Install-UvAstral $UvDir $tmp }
                'github' { Install-UvGitHub $UvDir $tmp $DownloadUrl }
            }
            $uv = Find-Uv -UvDir $UvDir -SkipExisting:($source -ne 'winget')
            if ($uv) { return $uv }
            if ($source -eq 'github' -and (Test-Path -LiteralPath (Join-Path $UvDir 'uv.exe'))) {
                $script:UvProblems.Add('uv was unpacked but does not start. An antivirus possibly blocks it.')
            }
        }
        return $null
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Set-UvProxy {
    # uv reads HTTPS_PROXY only; take the Windows proxy setting when there is one
    if ($env:HTTPS_PROXY -or $env:https_proxy) { return }
    try {
        $target = [Uri]'https://pypi.org/'
        $proxy = [Net.WebRequest]::GetSystemWebProxy().GetProxy($target)
        if ($proxy -and $proxy.Host -ne $target.Host) {
            $env:HTTPS_PROXY = $proxy.AbsoluteUri
            $env:HTTP_PROXY = $proxy.AbsoluteUri
            Write-Host "      using the Windows proxy $($proxy.Authority)"
        }
    } catch { }
}
