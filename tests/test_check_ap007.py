import shutil
import subprocess

import pytest

from agentprint.checks import SCAN_CHECKS, ap007_local_not_ignored
from agentprint.checks.ap007_local_not_ignored import CHECK as AP007
from agentprint.models import Severity
from agentprint.scanner import scan

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def isolated_git(tmp_path, monkeypatch):
    """Keep the developer's own global git config (and its excludes) out of these tests."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # no repo above tmp
    return tmp_path / "xdg"


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _rel(location: str) -> str:
    """The finding's path from the project folder, with forward slashes on every OS."""
    return location.replace("\\", "/").rsplit("agents-proj/", 1)[1]


def _ap007(findings):
    return sorted((_rel(f.location), f.detail) for f in findings if f.check_id == "AP007")


def test_ap007_is_registered():
    assert AP007 in SCAN_CHECKS
    assert (AP007.id, AP007.severity) == ("AP007", Severity.LOW)


def test_not_a_git_repo_has_no_findings(agents_project, isolated_git):
    assert _ap007(scan(None, agents_project)) == []


def test_personal_files_not_ignored_are_reported(agents_project, isolated_git):
    (agents_project / ".claude").mkdir()
    (agents_project / ".claude" / "settings.local.json").write_text("{}")
    (agents_project / "CLAUDE.local.md").write_text("my notes\n")
    _git(agents_project, "init", "-q")
    findings = [f for f in scan(None, agents_project) if f.check_id == "AP007"]
    assert sorted(_rel(f.location) for f in findings) == [
        ".claude/settings.local.json",
        ".gemini/.env",
        "CLAUDE.local.md",
    ]
    env = next(f for f in findings if f.location.endswith(".env"))
    assert env.severity is Severity.LOW and env.line is None
    assert "not in .gitignore" in env.detail
    assert ".gitignore" in env.fix and ".gemini/.env" in env.fix


def test_gitignored_files_are_fine(agents_project, isolated_git):
    _git(agents_project, "init", "-q")
    (agents_project / ".gitignore").write_text(".gemini/.env\n")
    assert _ap007(scan(None, agents_project)) == []


def test_global_excludes_count_as_ignored(agents_project, isolated_git):
    (isolated_git / "git").mkdir(parents=True)
    (isolated_git / "git" / "ignore").write_text("**/.gemini/.env\n")
    _git(agents_project, "init", "-q")
    assert _ap007(scan(None, agents_project)) == []


def test_committed_personal_file_says_how_to_untrack(agents_project, isolated_git):
    _git(agents_project, "init", "-q")
    _git(agents_project, "add", ".gemini/.env")
    _git(agents_project, "commit", "-q", "-m", "oops")
    (agents_project / ".gitignore").write_text(".gemini/.env\n")  # ignoring doesn't untrack
    [(where, detail)] = _ap007(scan(None, agents_project))
    assert where == ".gemini/.env" and "committed" in detail
    [finding] = [f for f in scan(None, agents_project) if f.check_id == "AP007"]
    assert "git rm --cached -- .gemini/.env " in finding.fix  # shell-quoted only when needed


def test_project_in_a_subfolder_of_a_repo(tmp_path, agents_project, isolated_git):
    _git(tmp_path, "init", "-q")
    [finding] = [f for f in scan(None, agents_project) if f.check_id == "AP007"]
    assert "agents-proj/.gemini/.env" in finding.fix


def test_missing_git_fails_open(agents_project, isolated_git, monkeypatch):
    _git(agents_project, "init", "-q")

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(ap007_local_not_ignored.subprocess, "run", no_git)
    assert _ap007(scan(None, agents_project)) == []
