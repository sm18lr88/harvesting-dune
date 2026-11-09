<#
.SYNOPSIS
  Dot-source to get Find-Companion: the path of the installed gaming.tools companion
  (GT.Launcher.exe), wherever it was installed.
#>

function Find-Companion {
    # 1) a running companion knows exactly where it lives
    $running = Get-Process -Name 'GT.Launcher' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path } | Select-Object -First 1
    if ($running) { return $running.Path }

    # 2) the installer's own uninstall entry (per-machine or per-user install)
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    $entry = Get-ItemProperty $keys -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -eq 'gaming.tools Companion' } | Select-Object -First 1
    if ($entry) {
        foreach ($candidate in @($entry.DisplayIcon, (Join-Path ([string]$entry.InstallLocation) 'GT.Launcher.exe'))) {
            if ($candidate) {
                $candidate = ($candidate -split ',')[0].Trim('"')
                if (Test-Path -LiteralPath $candidate) { return $candidate }
            }
        }
    }

    # 3) the default install folder
    $default = Join-Path $env:ProgramFiles 'gaming_tools\Companion\GT.Launcher.exe'
    if (Test-Path -LiteralPath $default) { return $default }
    return $null
}
