# Release: checks of the release and CI workflows that publish and test what guildmates install.
"""The running app reads its version from pyproject.toml, so a release tag must match it; nothing is
attached to a release before the test suite has passed; CI runs on every push."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
POWERSHELL = shutil.which("powershell.exe")


def _jobs(workflow: str) -> dict[str, str]:
    text = (WORKFLOWS / workflow).read_text(encoding="utf8")
    blocks = re.split(r"\n  (?=[a-z][\w-]*:\s*\n)", text.split("\njobs:", 1)[1])
    return {b.split(":", 1)[0]: b for b in blocks if re.match(r"[a-z][\w-]*:", b)}


def _needs(job: str) -> list[str]:
    found = re.search(r"^    needs:\s*(.+)$", job, re.M)
    return re.findall(r"[\w-]+", found.group(1)) if found else []


def _tested_before(name: str, jobs: dict[str, str]) -> bool:
    return "uv run pytest" in jobs[name] or any(_tested_before(n, jobs) for n in _needs(jobs[name]))


def _version_scripts() -> list[str]:
    scripts = []
    for job in _jobs("release.yml").values():
        for step in re.finditer(r"- name: Version\n(?:(?!      - ).*\n)*?        run: \|\n((?:          .*\n|\s*\n)+)", job + "\n"):
            scripts.append("\n".join(line[10:] for line in step.group(1).splitlines()))
    return scripts


def _run_version_step(script: str, tag: str, tmp_path: Path) -> subprocess.CompletedProcess:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "harvesting-dune"\nversion = "0.1.0"\n', encoding="ascii")
    body = script.replace("${{ github.event.release.tag_name }}", tag).replace("${{ github.run_number }}", "7")
    (tmp_path / "step.ps1").write_text(body, encoding="ascii")
    out = tmp_path / "out.txt"
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                           f"$env:GITHUB_OUTPUT = '{out}'; $ErrorActionPreference = 'Stop'; & '{tmp_path / 'step.ps1'}'"],
                          cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_a_release_tag_that_differs_from_the_pyproject_version_stops_the_release(tmp_path):
    scripts = _version_scripts()
    assert scripts
    for script in scripts:
        assert _run_version_step(script, "v0.2.0", tmp_path).returncode != 0


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
def test_a_release_tag_that_matches_the_pyproject_version_names_the_build(tmp_path):
    for script in _version_scripts():
        result = _run_version_step(script, "v0.1.0", tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        raw = (tmp_path / "out.txt").read_bytes()
        # Windows PowerShell 5.1 appends with '>>' in UTF-16; the runner's pwsh appends UTF-8
        assert "version=0.1.0" in raw.decode("utf-16" if raw.startswith(b"\xff\xfe") else "utf8")


def test_every_job_that_attaches_to_a_release_waits_for_the_test_suite():
    jobs = _jobs("release.yml")
    uploaders = [name for name, job in jobs.items() if "gh release upload" in job]
    assert uploaders
    assert [name for name in uploaders if not _tested_before(name, jobs)] == []


def test_the_update_manifest_is_attached_after_the_zip_it_names():
    zip_job = _jobs("release.yml")["zip"]
    uploads = [line for line in zip_job.splitlines() if "gh release upload" in line]
    assert len(uploads) == 2 and "dist\\*.zip" in uploads[0] and "dist\\latest.json" in uploads[1]


def test_ci_runs_on_a_push_to_any_branch():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf8")
    triggers = text.split("\non:", 1)[1].split("\n\n", 1)[0]
    push = re.search(r"^  push:[^\n]*\n((?:    .*\n)*)", triggers + "\n", re.M)
    assert push is not None
    assert "branches" not in push.group(1)
