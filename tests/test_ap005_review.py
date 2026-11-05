"""AP005 is critical, like AP002, and scan and push report an MCP-config key the same way."""

import json
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from agentprint.checks.ap005_mcp_inline_secret import CHECK as AP005
from agentprint.cli import app
from agentprint.gitcheck import check_push
from agentprint.models import Severity
from agentprint.scanner import scan
from tests import fakes


def _mcp_project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    server = {"command": "gh-mcp", "env": {"GITHUB_TOKEN": fakes.GITHUB}}
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": server}}, indent=2))
    return root


def test_ap005_is_critical():
    assert AP005.severity is Severity.CRITICAL


def test_fail_on_critical_catches_an_mcp_config_key(tmp_path):
    root = _mcp_project(tmp_path)
    result = CliRunner().invoke(app, ["scan", str(root), "--no-home", "--fail-on", "critical"])
    assert result.exit_code == 1
    assert fakes.GITHUB not in result.output


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_push_reports_the_same_check_as_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = _mcp_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    scanned = {(f.check_id, f.severity) for f in scan(None, root)}
    pushed = {(f.check_id, f.severity) for f in check_push(root)}
    assert pushed == scanned == {("AP005", Severity.CRITICAL)}
