# Update: scripts/update.ps1 under Windows PowerShell 5.1, against a local release server and a fake install folder.
"""Never installs for real: the release's install.ps1 and the install folder's own install.ps1 are stubs that
only record their arguments, and the game process is a stub. The functions come from the script's AST, as in
test_watcher.py, so the script's mutex and log rotation stay out of the way."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
STUB = "Add-Content -LiteralPath $env:HD_CALLS -Value ('{0} ' + ($args -join ' '))\nexit [int]$env:HD_{0}_EXIT\n"
HARNESS = (
    "$ErrorActionPreference = 'Stop'\n"
    ". (Join-Path $env:HD_SCRIPTS 'app-files.ps1')\n"
    ". (Join-Path $env:HD_SCRIPTS 'uv-setup.ps1')\n"
    "$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:HD_SCRIPTS 'update.ps1'), [ref]$null, [ref]$null)\n"
    "foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {\n"
    "    . ([scriptblock]::Create($f.Extent.Text))\n"
    "}\n"
    "$repo = $env:HD_REPO; $StageDir = $env:HD_STAGE; $ManifestUrl = $env:HD_URL\n"
    "$Port = 9231; $CheckMinutes = 10; $RetryHours = 24; $logFile = Join-Path $repo 'data\\update.log'\n"
    "function Get-Process { [CmdletBinding()] param([string]$Name) if ($Name -eq $GameProcess -and $env:HD_GAME) { [pscustomobject]@{ Id = 7 } } }\n")


def _release(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("version.txt", f"{version}\r\n")
        z.writestr("scripts/install.ps1", STUB.replace("{0}", "NEW"))
    return buf.getvalue()


@contextmanager
def _release_server(files: dict[str, bytes]):
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            name = self.path.rsplit("/", 1)[-1]
            requests.append(name)
            body = files.get(name)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/releases/latest/download/latest.json", requests
    finally:
        server.shutdown()
        server.server_close()


def _install_folder(tmp_path) -> Path:
    repo = tmp_path / "inst"
    (repo / "scripts").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "harvesting-dune"\nversion = "0.1.0"\n', encoding="ascii")
    (repo / "scripts" / "install.ps1").write_text(STUB.replace("{0}", "OLD"), encoding="ascii")
    return repo


def _files(version: str = "0.2.0", sha256: str | None = None) -> dict[str, bytes]:
    pkg = _release(version)
    manifest = {"version": version, "sha256": sha256 or hashlib.sha256(pkg).hexdigest()}
    return {"latest.json": json.dumps(manifest).encode(), f"harvesting-dune-{version}.zip": pkg}


def _run(tmp_path, repo: Path, url: str, game: bool, new_exit: int = 0) -> subprocess.CompletedProcess:
    harness = tmp_path / "harness.ps1"
    harness.write_text(HARNESS + "try { Invoke-Update } catch { Write-Log \"update stopped: $($_.Exception.Message)\"; exit 1 }\n",
                       encoding="ascii")
    env = {**os.environ, "HD_SCRIPTS": str(SCRIPTS), "HD_REPO": str(repo), "HD_STAGE": str(tmp_path / "stage"), "HD_URL": url,
           "HD_CALLS": str(tmp_path / "calls.txt"), "HD_GAME": "1" if game else "", "HD_NEW_EXIT": str(new_exit), "HD_OLD_EXIT": "0",
           "NO_PROXY": "127.0.0.1,localhost"}
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
                          creationflags=subprocess.CREATE_NO_WINDOW, env=env)


def _calls(tmp_path) -> list[str]:
    calls = tmp_path / "calls.txt"
    return calls.read_text(encoding="utf8").splitlines() if calls.exists() else []


def test_only_a_newer_release_counts(tmp_path):
    pairs = [("0.2.0", "0.1.0"), ("0.10.0", "0.9.0"), ("0.1.0", "0.1.0-dev.7"), ("0.1.0", "0.1.0"),
             ("0.1.0", "0.2.0"), ("0.2.0-dev.1", "0.1.0"), ("bad", "0.1.0"), ("0.2.0", "")]
    harness = tmp_path / "newer.ps1"
    harness.write_text(HARNESS + "".join(f"Test-Newer '{r}' '{l}'\n" for r, l in pairs), encoding="ascii")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
                            creationflags=subprocess.CREATE_NO_WINDOW, env={**os.environ, "HD_SCRIPTS": str(SCRIPTS), "HD_REPO": str(tmp_path)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.split() == ["True", "True", "True", "False", "False", "False", "False", "False"]


def test_a_release_found_during_play_installs_only_after_the_game_closes(tmp_path):
    repo = _install_folder(tmp_path)
    with _release_server(_files()) as (url, requests):
        idle = _run(tmp_path, repo, url, game=False)
        assert idle.returncode == 0, idle.stdout + idle.stderr
        assert requests == []  # nothing is fetched while nobody plays
        for _ in range(2):
            playing = _run(tmp_path, repo, url, game=True)
            assert playing.returncode == 0, playing.stdout + playing.stderr
        assert requests == ["latest.json", "harvesting-dune-0.2.0.zip"]  # the second run is within the check interval
        assert _calls(tmp_path) == []
        assert (tmp_path / "stage" / "0.2.0" / "ready").exists()
        closed = _run(tmp_path, repo, url, game=False)
        assert closed.returncode == 0, closed.stdout + closed.stderr
        assert len(requests) == 2
    calls = _calls(tmp_path)
    assert len(calls) == 1 and calls[0].startswith(f"NEW -InstallDir {repo} -NonInteractive -InstallUv -Port 9231 "), calls
    assert (repo / "data" / "update-restart").read_text(encoding="ascii").strip() == "0.2.0"
    assert not (tmp_path / "stage" / "0.2.0").exists()
    assert "updated to 0.2.0" in (repo / "data" / "update.log").read_text(encoding="utf8")


def test_a_zip_that_does_not_match_its_sha256_is_never_installed(tmp_path):
    repo = _install_folder(tmp_path)
    with _release_server(_files(sha256="0" * 64)) as (url, _):
        playing = _run(tmp_path, repo, url, game=True)
        closed = _run(tmp_path, repo, url, game=False)
    assert playing.returncode == 1, playing.stdout + playing.stderr
    assert closed.returncode == 0, closed.stdout + closed.stderr
    assert _calls(tmp_path) == []
    assert not (tmp_path / "stage" / "0.2.0" / "ready").exists()
    assert "damaged or changed" in (repo / "data" / "update.log").read_text(encoding="utf8")


def test_a_failed_install_puts_the_running_version_back_and_waits_before_trying_again(tmp_path):
    repo = _install_folder(tmp_path)
    with _release_server(_files()) as (url, _):
        assert _run(tmp_path, repo, url, game=True).returncode == 0
        for _ in range(2):
            closed = _run(tmp_path, repo, url, game=False, new_exit=1)
            assert closed.returncode == 0, closed.stdout + closed.stderr
    calls = _calls(tmp_path)
    assert [c.split()[0] for c in calls] == ["NEW", "OLD"], calls
    assert f"-InstallDir {repo} -NonInteractive" in calls[1]
    assert (repo / "data" / "update-restart").read_text(encoding="ascii").strip() == "0.1.0 (put back: 0.2.0 failed)"
    assert "0.1.0 is back" in (repo / "data" / "update.log").read_text(encoding="utf8")


def test_a_development_copy_never_updates_itself(tmp_path):
    repo = _install_folder(tmp_path)
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="ascii")  # a worktree has a .git file
    with _release_server(_files()) as (url, requests):
        assert _run(tmp_path, repo, url, game=True).returncode == 0
        assert _run(tmp_path, repo, url, game=False).returncode == 0
    assert requests == [] and _calls(tmp_path) == []
