# Watcher: the autorun watcher, the doctor and the admin script, run piece by piece under Windows PowerShell 5.1.
"""Never runs a script for real (they stop running copies, change the autorun task and reach the VPS): each test
takes single functions or blocks from the script's AST and runs them in a scratch session against stubs."""

import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
# the admin script stays in the private repository: without it these tests would parse nothing and pass by luck
needs_deploy_script = pytest.mark.skipif(not (SCRIPTS / "sync-deploy.ps1").exists(),
                                         reason="needs scripts/sync-deploy.ps1: the admin script, not in the public repository")

PARSE = "$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:HD_SCRIPT, [ref]$null, [ref]$null)\n"
FUNCTIONS = (
    "foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {\n"
    "    . ([scriptblock]::Create($f.Extent.Text))\n"
    "}\n")
BLOCK = "function Get-Inner($block) { $t = $block.Extent.Text; [scriptblock]::Create($t.Substring(1, $t.Length - 2)) }\n"


def _ps(tmp_path, body: str, script: str, **env: str) -> subprocess.CompletedProcess:
    harness = tmp_path / "harness.ps1"
    harness.write_text(PARSE + FUNCTIONS + BLOCK + body, encoding="ascii")
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
                          creationflags=subprocess.CREATE_NO_WINDOW,
                          env={**os.environ, "HD_SCRIPT": str(SCRIPTS / script), **env})


WATCHER_STUBS = """
$global:clock = [datetime]'2026-01-01T12:00:00'
$global:companionUp = $true
$global:detUp = $false
$global:starts = 0
function Get-Date { $global:clock }
function Start-Sleep { param([int]$Seconds = 0, [int]$Milliseconds = 0) $global:clock = $global:clock.AddSeconds($Seconds).AddMilliseconds($Milliseconds) }
function Get-Process { if ($global:companionUp) { [pscustomobject]@{ Id = 7 } } }
function Get-DetectorProcess { if ($global:detUp) { [pscustomobject]@{ ProcessId = 42 } } }
function Test-Feed { $true }
function Test-Path { $true }
function Move-Item { }
function Get-SettingsState { $null }
function Start-Process { $global:starts++; $global:detUp = $true }
function Show-Problem { }
function Write-Log { }
$Port = 9231; $HttpPort = 8765; $PollSeconds = 2; $NoBrowser = $true
$repo = 'C:\\fake'; $detectorExe = 'C:\\fake\\.venv\\Scripts\\harvesting-dune.exe'
$nextStart = [datetime]::MinValue; $startedAt = $null; $quickDeaths = 0; $goneSince = $null
$noFeedSince = $null; $warnedNoFeed = $false; $warnedExe = $false; $warnedCrash = $false; $browserPending = $false
$step = Get-Inner ($ast.Find({ param($n) $n -is [System.Management.Automation.Language.WhileStatementAst] }, $false).Body)
"""


def test_a_companion_restart_starts_the_detector_without_the_old_crash_backoff(tmp_path):
    body = WATCHER_STUBS + (
        "for ($k = 0; $k -lt 4; $k++) {\n"  # four quick deaths: the backoff has grown to 80 s
        "    $before = $global:starts\n"
        "    for ($i = 0; $i -lt 400 -and $global:starts -eq $before; $i++) { . $step }\n"
        "    $global:detUp = $false\n"
        "}\n"
        ". $step\n"  # the watcher notices the death and backs off
        "$global:companionUp = $false; . $step\n"  # the user closes the companion ...
        "$global:companionUp = $true\n"  # ... and opens it again
        "$before = $global:starts; . $step\n"
        "\"started on the first poll: $($global:starts -gt $before)\"\n")
    result = _ps(tmp_path, body, "companion-autorun.ps1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "started on the first poll: True", result.stdout + result.stderr


def _sync_stubs(tmp_path, ssh_exit: int, agent_exit: int) -> dict[str, str]:
    key = tmp_path / "ops_ed25519"
    key.write_text("fake", encoding="ascii")
    ssh = tmp_path / "ssh.cmd"
    ssh.write_text(f"@echo off\r\necho ssh stub failed 1>&2\r\nexit /b {ssh_exit}\r\n", encoding="ascii")
    agent = tmp_path / "ssh-add.cmd"
    agent.write_text(f"@echo off\r\necho agent stub\r\nexit /b {agent_exit}\r\n", encoding="ascii")
    return {"HD_KEY": str(key), "HD_SSH": str(ssh), "HD_SSHADD": str(agent)}


SYNC_VARS = ("$ErrorActionPreference = 'Stop'\n"
             "$key = $env:HD_KEY; $ssh = $env:HD_SSH; $sshAdd = $env:HD_SSHADD; $Ip = '192.0.2.1'; $sshArgs = @()\n")


@needs_deploy_script
def test_deploy_probe_reports_an_unreachable_server_as_exit_1_without_the_unlock_hint(tmp_path):
    env = _sync_stubs(tmp_path, ssh_exit=255, agent_exit=0)  # the agent holds a key: the network is at fault
    result = _ps(tmp_path, SYNC_VARS + "Invoke-Probe\n", "sync-deploy.ps1", **env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Unlock the ops key" not in result.stderr


@needs_deploy_script
def test_deploy_probe_reports_an_empty_ssh_agent_as_a_locked_key(tmp_path):
    env = _sync_stubs(tmp_path, ssh_exit=255, agent_exit=1)  # ssh-add -l: "The agent has no identities."
    result = _ps(tmp_path, SYNC_VARS + "Invoke-Probe\n", "sync-deploy.ps1", **env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Unlock the ops key" in result.stderr


@needs_deploy_script
def test_deploy_keys_reports_a_keysctl_usage_error_as_exit_1(tmp_path):
    env = _sync_stubs(tmp_path, ssh_exit=2, agent_exit=0)  # keysctl: unknown key id
    body = SYNC_VARS + (
        "$Rest = @('revoke', 'k_unknown')\n"
        "$switch = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.SwitchStatementAst] }, $false)\n"
        "$keys = ($switch.Clauses | Where-Object { $_.Item1.Extent.Text -eq \"'keys'\" }).Item2\n"
        ". (Get-Inner $keys)\n")
    result = _ps(tmp_path, body, "sync-deploy.ps1", **env)
    assert result.returncode == 1, result.stdout + result.stderr


class _ListHandler(BaseHTTPRequestHandler):
    targets: list = []

    def do_GET(self):  # noqa: N802
        data = json.dumps(self.targets).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@contextmanager
def _devtools_list(targets: list):
    handler = type("Handler", (_ListHandler,), {"targets": targets})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_doctor_does_not_count_a_dune_service_worker_as_a_companion_dune_page(tmp_path):
    targets = [{"type": "service_worker", "url": "https://app.gaming.tools/dune/sw.js"},
               {"type": "page", "url": "https://app.gaming.tools/home"}]
    body = (
        "function Check([string]$name, [string]$status, [string]$detail) { \"$name|$status|$detail\" }\n"
        "$Port = [int]$env:HD_PORT; $version = $true\n"
        "$if = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and "
        "$n.Clauses[0].Item1.Extent.Text -eq '$version' }, $true)\n"
        ". (Get-Inner $if.Clauses[0].Item2)\n")
    with _devtools_list(targets) as port:
        result = _ps(tmp_path, body, "doctor.ps1", HD_PORT=str(port))
    assert result.returncode == 0, result.stdout + result.stderr
    pages = [line for line in result.stdout.splitlines() if line.startswith("Companion Dune pages|")]
    assert pages == ["Companion Dune pages|WARN|no Dune map or minimap page open"], result.stdout + result.stderr
