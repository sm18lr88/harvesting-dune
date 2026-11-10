<#
.SYNOPSIS
  Makes harvesting-dune start and stop with the gaming.tools companion.

.DESCRIPTION
  1. Writes the per-user, per-app WebView2 policy
       HKCU\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments
         GT.Launcher.exe = --remote-debugging-port=<Port>
     so every companion launch exposes the localhost DevTools feed (no -Restart step).
     It applies to GT.Launcher.exe only; other WebView2 apps are unaffected.
  2. Registers a logon scheduled task that runs companion-autorun.ps1 hidden.

  -Uninstall removes both. The companion keeps any port it already has until it restarts.
#>
param(
    [int]$Port = 9231,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'

$taskName = 'harvesting-dune autorun'
$exe = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\harvesting-dune.exe'
$policyKey = 'HKCU:\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments'
$watcher = Join-Path $PSScriptRoot 'companion-autorun.ps1'

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    # IndexOf, not -notlike: a path with [ ] would be read as a wildcard class and never match itself
    if ($task -and ([string]($task.Actions | Select-Object -First 1).Arguments).IndexOf($watcher, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        # another copy of harvesting-dune owns autorun: removing it would break that copy
        Write-Host 'The autorun task belongs to another copy of harvesting-dune. It stays as it is.'
        return
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path $policyKey) { Remove-ItemProperty -Path $policyKey -Name 'GT.Launcher.exe' -ErrorAction SilentlyContinue }
    Write-Host 'Removed the autorun task and the companion DevTools policy.'
    return
}

if (-not (Test-Path -LiteralPath $exe)) {
    throw "The detector is not installed yet ($exe missing). Run scripts\install.ps1 instead."
}
# the tool was called dune-detector before: its task would start a second watcher
Unregister-ScheduledTask -TaskName 'dune-detector autorun' -Confirm:$false -ErrorAction SilentlyContinue
if (-not (Test-Path $policyKey)) { New-Item -Path $policyKey -Force | Out-Null }
Set-ItemProperty -Path $policyKey -Name 'GT.Launcher.exe' -Value "--remote-debugging-port=$Port"

# conhost --headless keeps the console window from flashing at logon.
$action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument (
    "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$watcher`" -Port $Port")
# the signed-in identity as Windows names it (works for local, Microsoft and work accounts)
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null

Write-Host "Installed: companion DevTools policy (port $Port) and logon task '$taskName'."
