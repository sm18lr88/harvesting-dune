<#
.SYNOPSIS
  Dot-source for the guild key: asks for it with hidden input, checks it with the sync server, and saves
  it with the detector's own code (Detector.set_sync_key), so settings.py stays the only writer of
  data\settings.json and the only judge of the key format. The key goes to curl and to Python on stdin:
  it is never on a command line, on screen or in the install log.
#>

function Test-GuildKeySaved([string]$repo) {
    $file = Join-Path $repo 'data\settings.json'
    if (-not (Test-Path -LiteralPath $file)) { return $false }
    try {
        $s = Get-Content -LiteralPath $file -Raw -ErrorAction Stop | ConvertFrom-Json
        return [bool]$s.syncKey
    } catch { return $false }
}

function Read-Secret([string]$prompt) {
    $secure = Read-Host -Prompt $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Save-GuildKey([string]$repo, [string]$key) {
    # 0 saved, 3 not a guild key, anything else: the helper failed
    $ErrorActionPreference = 'Continue'
    $python = Join-Path $repo '.venv\Scripts\python.exe'
    # bytes, ASCII only: with a UTF-8 console, the pipe puts a byte order mark before the key
    $code = 'import sys; from pathlib import Path; from harvesting_dune.app import Detector; ' +
        'key = sys.stdin.buffer.readline().decode(''ascii'', ''ignore'').strip(); ' +
        'det = Detector(None, data_dir=Path(sys.argv[1])); ' +
        'sys.exit(0 if det.set_sync_key(key) else 3)'
    $out = $key | & $python -I -c $code (Join-Path $repo 'data') 2>&1
    $exit = $LASTEXITCODE
    if ($exit -ne 0 -and $exit -ne 3) {
        $out | ForEach-Object { "$_" } | Where-Object { $_.IndexOf($key) -lt 0 } | Select-Object -Last 3 |
            ForEach-Object { Write-Host "      $_" }
    }
    return $exit
}

function Test-GuildKeyOnServer([string]$key, [string]$server, [string]$caFile) {
    # 'ok' (the server accepts the key), 'rejected' (401: unknown, revoked or expired), 'unreachable' (anything else).
    # curl.exe ships with Windows 10 (1803) and later. Its settings come on stdin, so the key stays off the command line.
    $curl = "$env:SystemRoot\System32\curl.exe"
    if (-not (Test-Path -LiteralPath $curl)) { return 'unreachable' }
    $ErrorActionPreference = 'Continue'
    $config = "url = `"$server/v1/data`"`r`nheader = `"Authorization: Bearer $key`"`r`n"
    # --ssl-no-revoke: schannel cannot reach the CRL/OCSP servers for a private CA and fails the handshake otherwise.
    # A UTF-8 console makes PowerShell 5.1 put a BOM before piped text, and curl then rejects the first option.
    $savedPipe, $savedConsole = $global:OutputEncoding, [Console]::InputEncoding
    $noBom = New-Object System.Text.UTF8Encoding $false
    try {
        $global:OutputEncoding = $noBom
        [Console]::InputEncoding = $noBom
        $code = $config | & $curl --config - --cacert $caFile --ssl-no-revoke --silent --show-error --connect-timeout 10 --max-time 10 --output NUL --write-out '%{http_code}' 2>$null
        $exit = $LASTEXITCODE
    } finally {
        $global:OutputEncoding = $savedPipe
        [Console]::InputEncoding = $savedConsole
    }
    if ($exit -eq 0 -and "$code" -eq '200') { return 'ok' }
    if ($exit -eq 0 -and "$code" -eq '401') { return 'rejected' }
    return 'unreachable'
}

function Use-GuildKey([string]$repo, [string]$key, [string]$server, [string]$caFile) {
    # 'saved', 'rejected', 'not-a-key' or 'failed'. Nothing is saved unless the server accepts the key or cannot be reached.
    $check = Test-GuildKeyOnServer $key $server $caFile
    if ($check -eq 'rejected') { return 'rejected' }
    $result = Save-GuildKey $repo $key
    if ($result -eq 3) { return 'not-a-key' }
    if ($result -ne 0) { return 'failed' }
    if ($check -eq 'ok') {
        Write-Host '      Guild key saved. The server accepted it.' -ForegroundColor Green
    } else {
        Write-Host '      Guild key saved. The server could not be reached, so the key is checked when the detector starts.' -ForegroundColor Yellow
    }
    return 'saved'
}

function Request-GuildKey([string]$repo, [bool]$required, [string]$server, [string]$caFile) {
    # true when a key is saved (or none is needed), false after 3 refused keys or a failed save
    $saved = Test-GuildKeySaved $repo
    if ($saved) {
        Write-Host '      A guild key is already saved. Press Enter to keep it, or paste a new key.'
    } else {
        Write-Host '      Your guild admin gives you a guild key. Paste it here. It stays hidden.'
    }
    for ($try = 1; $try -le 3; $try++) {
        $key = (Read-Secret '      Guild key').Trim()
        if (-not $key) {
            if ($saved) { Write-Host '      no new key.'; return $true }
            if (-not $required) { Write-Host '      no key. You can add the key later on the settings page.'; return $true }
            Write-Host '      A guild key is necessary. Paste it, then press Enter.' -ForegroundColor Yellow
            continue
        }
        $outcome = Use-GuildKey $repo $key $server $caFile
        $key = $null
        if ($outcome -eq 'saved') { return $true }
        if ($outcome -eq 'rejected') { Write-Host '      key rejected or expired: ask your guild admin' -ForegroundColor Yellow; continue }
        if ($outcome -eq 'not-a-key') {
            Write-Host '      That is not a guild key. A guild key starts with gs1_. Copy it again, and paste it.' -ForegroundColor Yellow
            continue
        }
        Write-Host '      The key was not saved. Add it later on the settings page.' -ForegroundColor Yellow
        return (-not $required)
    }
    return $false
}

function Set-PresetGuildKey([string]$repo, [string]$key, [string]$server, [string]$caFile) {
    # -GuildKey or HARVESTING_DUNE_KEY: no questions. true when the key is saved, false when it is refused or not saved
    $outcome = Use-GuildKey $repo $key.Trim() $server $caFile
    if ($outcome -eq 'rejected') { Write-Host '      key rejected or expired: ask your guild admin' -ForegroundColor Yellow }
    if ($outcome -eq 'not-a-key') { Write-Host '      That is not a guild key. A guild key starts with gs1_.' -ForegroundColor Yellow }
    if ($outcome -eq 'failed') { Write-Host '      The key was not saved. Add it later on the settings page.' -ForegroundColor Yellow }
    return ($outcome -eq 'saved')
}
