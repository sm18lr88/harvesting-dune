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

function Invoke-KeyHelper([string]$repo, [string]$key, [string]$code) {
    # runs $code with the key on stdin; 0 done, 3 not a guild key, anything else: the helper failed
    $ErrorActionPreference = 'Continue'
    $python = Join-Path $repo '.venv\Scripts\python.exe'
    # bytes, ASCII only: with a UTF-8 console, the pipe puts a byte order mark before the key
    $code = 'import sys; key = sys.stdin.buffer.readline().decode(''ascii'', ''ignore'').strip(); ' + $code
    $out = $key | & $python -I -c $code (Join-Path $repo 'data') 2>&1
    $exit = $LASTEXITCODE
    if ($exit -ne 0 -and $exit -ne 3) {
        $out | ForEach-Object { "$_" } | Where-Object { $_.IndexOf($key) -lt 0 } | Select-Object -Last 3 |
            ForEach-Object { Write-Host "      $_" }
    }
    return $exit
}

function Test-GuildKeyFormat([string]$repo, [string]$key) {
    # before the server check: the server answers 401 to a mistyped key as to a revoked one
    return Invoke-KeyHelper $repo $key 'from harvesting_dune.settings import KEY_RE; sys.exit(0 if KEY_RE.fullmatch(key) else 3)'
}

function Save-GuildKey([string]$repo, [string]$key) {
    # set_sync_key returns True even when settings.json could not be written: read the file back (4)
    return Invoke-KeyHelper $repo $key ('from pathlib import Path; from harvesting_dune.app import Detector; ' +
        'from harvesting_dune.fsutil import read_json; det = Detector(None, data_dir=Path(sys.argv[1])); ' +
        'ok = det.set_sync_key(key); ' +
        'sys.exit(3 if not ok else 0 if read_json(det.settings_path, dict, {}).get(''syncKey'') == key else 4)')
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
    $format = Test-GuildKeyFormat $repo $key
    if ($format -eq 3) { return 'not-a-key' }
    if ($format -ne 0) { return 'failed' }
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
        if ($required) { Write-Host '      The key was not saved. The reason is above.' -ForegroundColor Yellow; return $false }
        Write-Host '      The key was not saved. Add it later on the settings page.' -ForegroundColor Yellow
        return $true
    }
    return $false
}

function Set-PresetGuildKey([string]$repo, [string]$key, [string]$server, [string]$caFile) {
    # -GuildKey or HARVESTING_DUNE_KEY: no questions. true when the key is saved, false when it is refused or not saved
    $outcome = Use-GuildKey $repo $key.Trim() $server $caFile
    if ($outcome -eq 'rejected') { Write-Host '      key rejected or expired: ask your guild admin' -ForegroundColor Yellow }
    if ($outcome -eq 'not-a-key') { Write-Host '      That is not a guild key. A guild key starts with gs1_.' -ForegroundColor Yellow }
    if ($outcome -eq 'failed') { Write-Host '      The key was not saved. The reason is above.' -ForegroundColor Yellow }
    return ($outcome -eq 'saved')
}

function Invoke-GuildKeyStep([string]$repo, [string]$presetKey, [bool]$interactive, [string]$server, [string]$caFile) {
    # the install never finishes without a guild key: returns (reason, fix) when it must stop, nothing when a key is saved
    $askAdmin = 'Ask your guild admin for the key. Then run the installer again.'
    if ($presetKey) {
        if (Set-PresetGuildKey $repo $presetKey $server $caFile) { return }
        return @('a valid guild key is necessary.', $askAdmin)
    }
    if (-not $interactive) {
        if (Test-GuildKeySaved $repo) { Write-Host '      a guild key is already saved.'; return }
        return @('a guild key is necessary.', 'Run Install.cmd and paste the key, or run the installer with -GuildKey <key> (or set HARVESTING_DUNE_KEY).')
    }
    if (Request-GuildKey $repo $true $server $caFile) { return }
    return @('a valid guild key is necessary.', $askAdmin)
}
