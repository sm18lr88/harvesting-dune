# Installer: checks of the release zip and the scripts that a guildmate runs.
"""cmd.exe and Windows PowerShell 5.1 misread non-ASCII text, and cmd.exe misreads LF-only batch
files; the zip must hold exactly the scripts that install.ps1 dot-sources."""

import os
import re
import shutil
import socket
import subprocess
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


def test_install_requires_the_guild_key():
    text = (ROOT / "scripts" / "install.ps1").read_text(encoding="ascii")
    assert re.search(r"^\$GuildKeyRequired = \$true\b", text, re.M)


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
        env = {**os.environ, "HD_GOOD": good, "HD_BAD": "gs1_unknown_nope", "HD_CA": str(CERTS / "test-ca.pem"),
               "HD_SERVER": f"https://127.0.0.1:{port}", "HD_DEAD": f"https://127.0.0.1:{dead_port}"}
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(check), str(GUILD_KEY)],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "ok rejected unreachable"
