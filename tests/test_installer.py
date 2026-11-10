# Installer: checks of the release zip and the scripts that a guildmate runs.
"""cmd.exe and Windows PowerShell 5.1 misread non-ASCII text, and cmd.exe misreads LF-only batch
files; the zip must hold exactly the scripts that install.ps1 dot-sources."""

import _winapi
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from guildsync.api import Api
from guildsync.httpd import make_server
from guildsync.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
CERTS = ROOT / "tests" / "certs"
GUILD_KEY = ROOT / "scripts" / "guild-key.ps1"
POWERSHELL = shutil.which("powershell.exe")
VENV_PYTHON = Path(sys.prefix) / "Scripts" / "python.exe"
LOOPBACK = "127.0.0.1,localhost"  # NO_PROXY: a proxy set on this PC must not carry the test's local requests


def _powershell(script: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, env=env)


@contextmanager
def _install_folder_with_this_venv(tmp_path):
    """guild-key.ps1 runs <install>\\.venv\\Scripts\\python.exe: link it to this run's environment."""
    repo = tmp_path / "inst"
    repo.mkdir()
    venv = repo / ".venv"
    _winapi.CreateJunction(sys.prefix, str(venv))
    try:
        yield repo
    finally:
        os.rmdir(venv)  # removes the link only, never the environment behind it


def _user_scripts() -> list[str]:
    text = (ROOT / "scripts" / "app-files.ps1").read_text(encoding="ascii")
    block = re.search(r"\$UserScripts = @\((.*?)\)", text, re.S)
    assert block
    return re.findall(r"'([^']+\.ps1)'", block.group(1))


def test_user_scripts_are_ascii_and_cover_every_dot_sourced_file():
    names = _user_scripts()
    for name in names:
        text = (ROOT / "scripts" / name).read_text(encoding="ascii")  # raises on non-ASCII
        for sourced in re.findall(r"Join-Path \$PSScriptRoot '([^']+\.ps1)'", text):
            assert sourced in names, f"{name} needs {sourced} in the zip"


def test_cmd_files_are_ascii():
    for name in ("Install.cmd", "Uninstall.cmd"):
        (ROOT / name).read_bytes().decode("ascii")


def _release_job(name: str) -> str:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf8")
    jobs = re.split(r"\n  (?=[a-z][\w-]*:\s*\n)", text.split("\njobs:", 1)[1])
    return next(j for j in jobs if j.startswith(f"{name}:"))


def test_inno_installer_copies_only_the_user_scripts():
    iss = (ROOT / "installer" / "harvesting-dune.iss").read_text(encoding="utf8")
    assert sorted(re.findall(r'Source: "\.\.\\scripts\\([^"]+)"', iss)) == sorted(_user_scripts())


def test_a_silent_inno_install_never_waits_for_a_key_and_fails_visibly():
    code = (ROOT / "installer" / "harvesting-dune.iss").read_text(encoding="utf8").split("[Code]", 1)[1]
    assert re.search(r"if WizardSilent then\s+Result := Result \+ ' -NonInteractive'", code)
    assert re.search(r"function GetCustomSetupExitCode\(\): Integer;\s*begin\s*Result := SetupExitCode;", code)


def test_release_install_tests_check_the_key_with_a_local_guild_server():
    zip_job = _release_job("zip")
    assert zip_job.index("uv sync") < zip_job.index("test-zip.ps1") and "-LocalGuildPython" in zip_job
    installer_job = _release_job("installer")
    assert "HARVESTING_DUNE_KEY" in installer_job and "/SYNCSERVER=" in installer_job


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_build_zip_holds_only_user_files(tmp_path):
    subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "installer" / "build-zip.ps1"), "-Version", "9.9.9-test", "-OutDir", str(tmp_path)],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
    with zipfile.ZipFile(tmp_path / "harvesting-dune-9.9.9-test.zip") as z:
        names = set(z.namelist())
        assert {"Install.cmd", "Uninstall.cmd", "pyproject.toml", "uv.lock", "README.md", "version.txt"} <= names
        assert {f"scripts/{s}" for s in _user_scripts()} == {n for n in names if n.startswith("scripts/")}
        assert "src/harvesting_dune/cli.py" in names
        assert not any("__pycache__" in n or "\\" in n or n.startswith(("tests/", "server/")) for n in names)
        assert z.read("version.txt").strip() == b"9.9.9-test"
        cmd = z.read("Install.cmd")
        assert cmd.count(b"\n") == cmd.count(b"\r\n")


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_install_requires_the_guild_key(tmp_path):
    check = tmp_path / "step.ps1"
    check.write_text(
        ". $args[0]\n"
        "function Read-Secret([string]$prompt) { '' }\n"  # the player presses Enter without a key
        "$asked = @(Invoke-GuildKeyStep $args[1] '' $true 'https://127.0.0.1:1' 'none')\n"
        "$batch = @(Invoke-GuildKeyStep $args[1] '' $false 'https://127.0.0.1:1' 'none')\n"
        "\"$($asked.Count) $($batch.Count)\"\n", encoding="ascii")
    (tmp_path / "inst").mkdir()
    result = _powershell(check, str(GUILD_KEY), str(tmp_path / "inst"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "2 2", result.stdout + result.stderr  # (reason, fix): the install stops
    assert re.search(r"^\$keyProblem = Invoke-GuildKeyStep ", (ROOT / "scripts" / "install.ps1").read_text(encoding="ascii"), re.M)


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_every_script_parses_under_windows_powershell(tmp_path):
    check = tmp_path / "parse.ps1"
    check.write_text(
        "$bad = @()\n"
        "foreach ($dir in @((Join-Path $args[0] 'scripts'), (Join-Path $args[0] 'installer'))) {\n"
        "    foreach ($f in Get-ChildItem -LiteralPath $dir -Filter *.ps1) {\n"
        "        $errs = $null\n"
        "        [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errs)\n"
        "        if ($errs) { $bad += $f.Name + ': ' + ($errs -join '; ') }\n"
        "    }\n"
        "}\n"
        "$bad\n"
        "exit $bad.Count\n", encoding="ascii")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(check), str(ROOT)],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


@contextmanager
def _local_sync_server(tmp_path):
    clock = time.time
    storage = Storage.open(str(tmp_path / "guildsync.sqlite"), clock)
    server = make_server(storage, str(CERTS / "test-server.pem"), str(CERTS / "test-server.key"),
                         "127.0.0.1", 0, Api(storage, clock), now=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], storage
    finally:
        server.shutdown()
        server.server_close()
        storage.close()


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_guild_key_check_against_a_local_server(tmp_path):
    check = tmp_path / "check.ps1"
    check.write_text(
        ". $args[0]\n"
        "$ok = Test-GuildKeyOnServer $env:HD_GOOD $env:HD_SERVER $env:HD_CA\n"
        "$bad = Test-GuildKeyOnServer $env:HD_BAD $env:HD_SERVER $env:HD_CA\n"
        "$dead = Test-GuildKeyOnServer $env:HD_GOOD $env:HD_DEAD $env:HD_CA\n"
        "\"$ok $bad $dead\"\n", encoding="ascii")
    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    with _local_sync_server(tmp_path) as (port, storage):
        _, good = storage.issue_key("Paul")
        dead_proxy = f"http://127.0.0.1:{dead_port}"  # as on a PC behind a proxy: the checks talk to this PC only
        env = {**os.environ, "HTTPS_PROXY": dead_proxy, "ALL_PROXY": dead_proxy, "NO_PROXY": LOOPBACK,
               "HD_GOOD": good, "HD_BAD": "gs1_unknown_nope", "HD_CA": str(CERTS / "test-ca.pem"),
               "HD_SERVER": f"https://127.0.0.1:{port}", "HD_DEAD": f"https://127.0.0.1:{dead_port}"}
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(check), str(GUILD_KEY)],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "ok rejected unreachable"


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_uninstall_from_a_custom_install_folder_removes_that_folder(tmp_path):
    inst = tmp_path / "custom"
    shutil.copytree(ROOT / "scripts", inst / "scripts")
    (inst / ".harvesting-dune-install").write_text("", encoding="ascii")
    called = tmp_path / "autorun-uninstall-called"
    # a stub: the real script would touch this PC's autorun task and WebView2 policy
    (inst / "scripts" / "install-autorun.ps1").write_text(f"New-Item -ItemType File -Path '{called}' | Out-Null\n", encoding="ascii")
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path / "lad")}  # the default folder does not exist
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                             str(inst / "scripts" / "install.ps1"), "-Uninstall", "-FromPackage", "-LogFile", str(tmp_path / "u.log")],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert called.exists(), result.stdout
    assert not (inst / ".harvesting-dune-install").exists(), result.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_no_autorun_install_removes_this_folders_old_task(tmp_path):
    repo = tmp_path / "inst"
    (repo / "scripts").mkdir(parents=True)
    called = tmp_path / "autorun-called"
    # a stub: the real script would touch this PC's autorun task and WebView2 policy
    (repo / "scripts" / "install-autorun.ps1").write_text(f"Set-Content -LiteralPath '{called}' -Value \"$args\"\n", encoding="ascii")
    check = tmp_path / "remove.ps1"
    check.write_text(
        ". $args[0]\n"
        "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName)\n"
        "    [pscustomobject]@{ Actions = @([pscustomobject]@{ Arguments = $env:HD_TASK_ARGS }) } }\n"
        "Remove-OwnAutorun $args[1]\n", encoding="ascii")

    def run(watcher: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "HD_TASK_ARGS": f'--headless powershell.exe -File "{watcher}" -Port 9231'}
        return _powershell(check, str(ROOT / "scripts" / "app-files.ps1"), str(repo), env=env)

    other = run(tmp_path / "other" / "scripts" / "companion-autorun.ps1")
    assert other.returncode == 0, other.stdout + other.stderr
    assert not called.exists(), "another folder's autorun task must stay"
    ours = run(repo / "scripts" / "companion-autorun.ps1")
    assert ours.returncode == 0, ours.stdout + ours.stderr
    assert called.read_text(encoding="ascii").strip() == "-Uninstall"
    assert re.search(r"^if \(\$NoAutorun\) \{\s*\n\s*Write-Host '      skipped[^\n]*\n\s*Remove-OwnAutorun \$repo\n\} else \{",
                     (ROOT / "scripts" / "install.ps1").read_text(encoding="ascii"), re.M)


@pytest.mark.skipif(POWERSHELL is None or not VENV_PYTHON.exists(), reason="needs Windows PowerShell and a venv")
def test_a_mistyped_guild_key_is_reported_as_not_a_key(tmp_path):
    check = tmp_path / "use.ps1"
    check.write_text(". $args[0]\nUse-GuildKey $args[1] $env:HD_KEY $env:HD_SERVER $env:HD_CA\n", encoding="ascii")
    with _install_folder_with_this_venv(tmp_path) as repo, _local_sync_server(tmp_path) as (port, storage):
        _, good = storage.issue_key("Paul")
        env = {**os.environ, "NO_PROXY": LOOPBACK, "HD_KEY": good + ".", "HD_CA": str(CERTS / "test-ca.pem"),
               "HD_SERVER": f"https://127.0.0.1:{port}"}
        result = _powershell(check, str(GUILD_KEY), str(repo), env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "not-a-key", result.stdout + result.stderr


@pytest.mark.skipif(POWERSHELL is None or not VENV_PYTHON.exists(), reason="needs Windows PowerShell and a venv")
def test_a_guild_key_that_could_not_be_written_is_not_reported_saved(tmp_path):
    check = tmp_path / "use.ps1"
    check.write_text(". $args[0]\nUse-GuildKey $args[1] $env:HD_KEY $env:HD_SERVER $env:HD_CA\n", encoding="ascii")
    with socket.socket() as s:  # a port nobody listens on: the key is saved without the server check
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    with _install_folder_with_this_venv(tmp_path) as repo:
        settings = repo / "data" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"lines": true}', encoding="utf8")
        settings.chmod(stat.S_IREAD)
        try:
            env = {**os.environ, "NO_PROXY": LOOPBACK, "HD_KEY": "gs1_abcdefgh_" + "A" * 32, "HD_CA": str(CERTS / "test-ca.pem"),
                   "HD_SERVER": f"https://127.0.0.1:{dead_port}"}
            result = _powershell(check, str(GUILD_KEY), str(repo), env=env)
        finally:
            settings.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "failed", result.stdout + result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_an_old_uv_is_not_used(tmp_path):
    old, new = tmp_path / "old" / "uv.cmd", tmp_path / "new" / "uv.cmd"
    for stub, version in ((old, "0.1.0"), (new, "0.13.7")):
        stub.parent.mkdir()
        stub.write_text(f"@echo uv {version} (stub)\r\n", encoding="ascii")
    check = tmp_path / "uv.ps1"
    check.write_text(". $args[0]\n\"$(Test-Uv $args[1]) $(Test-Uv $args[2])\"\n", encoding="ascii")
    result = _powershell(check, str(ROOT / "scripts" / "uv-setup.ps1"), str(old), str(new))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "False True"


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_uninstall_of_a_folder_that_does_not_exist_leaves_it_missing(tmp_path):
    pkg = tmp_path / "pkg"
    shutil.copytree(ROOT / "scripts", pkg / "scripts")
    (pkg / "scripts" / "install-autorun.ps1").write_text("exit 7\n", encoding="ascii")  # never the real autorun
    missing = tmp_path / "typo"
    (tmp_path / "temp").mkdir()
    env = {**os.environ, "TEMP": str(tmp_path / "temp"), "TMP": str(tmp_path / "temp")}
    result = _powershell(pkg / "scripts" / "install.ps1", "-Uninstall", "-InstallDir", str(missing), env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is not installed in" in result.stdout, result.stdout
    assert not missing.exists()
