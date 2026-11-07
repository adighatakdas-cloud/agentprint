import json
import shutil
import subprocess

import pytest

from agentprint.checks.ap007_local_not_ignored import LocalFileNotIgnored
from agentprint.gitcheck import check_push
from tests import fakes
from tests.conftest import set_home

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # Keep the machine's own git config and global ignore file out of the test.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    set_home(monkeypatch, str(tmp_path / "home"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "index.js").write_text("1\n")
    return root


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_clean_repo_has_no_findings(repo):
    _git(repo, "add", "-A")
    assert check_push(repo) == []


def test_not_a_repo_has_no_findings(tmp_path):
    assert check_push(tmp_path) == []


def test_tracked_local_settings_with_key_blocks(repo):
    allow = [f"Bash(export K={fakes.ANTHROPIC})"]
    _write(repo / ".claude" / "settings.local.json", json.dumps({"permissions": {"allow": allow}}))
    _git(repo, "add", "-A")
    findings = check_push(repo / ".claude")  # from a subfolder: still the whole repo
    assert sorted((f.check_id, f.severity.name) for f in findings) == [
        ("AP002", "CRITICAL"),
        ("AP006", "HIGH"),
    ]
    assert all(f.location == str(repo / ".claude" / "settings.local.json") for f in findings)
    assert all("would be pushed" in f.title or "Secret" in f.title for f in findings)
    assert fakes.ANTHROPIC not in repr(findings)


def test_key_in_tracked_shared_settings_blocks(repo):
    _write(repo / ".mcp.json", json.dumps({"mcpServers": {"x": {"env": {"T": fakes.GITHUB}}}}))
    _git(repo, "add", "-A")
    [finding] = check_push(repo)
    assert finding.check_id == "AP005" and finding.location == str(repo / ".mcp.json")


def test_untracked_shared_settings_are_not_reported_as_pushed(repo):
    _write(repo / ".mcp.json", json.dumps({"mcpServers": {"x": {"env": {"T": fakes.GITHUB}}}}))
    _write(repo / ".gitignore", ".mcp.json\n")
    _git(repo, "add", ".gitignore")
    assert check_push(repo) == []


def test_agent_file_not_ignored_matches_the_ap007_check(repo):
    _write(repo / ".claude" / "settings.local.json", "{}")
    [finding] = check_push(repo)
    check = LocalFileNotIgnored
    assert (finding.check_id, finding.severity, finding.title) == (
        check.id,
        check.severity,
        check.title,
    )


def test_ignored_agent_file_is_fine(repo):
    _write(repo / ".claude" / "settings.local.json", "{}")
    _write(repo / ".gitignore", ".claude/settings.local.json\n")
    _git(repo, "add", ".gitignore")
    assert check_push(repo) == []
