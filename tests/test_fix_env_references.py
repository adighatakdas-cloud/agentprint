"""agentprint fix moves an inline MCP env key to an environment-variable reference only in the
files of agents that document one: VS Code "${env:NAME}", Qwen Code, Amp, Factory Droid and
Augment "${NAME}", Kilo Code "{env:NAME}", Crush "$NAME" (Claude Code's "${NAME}" is in
test_fix_changes.py). Every other agent's MCP key, and every header, stays with the human."""

import json
import stat
from pathlib import Path

import pytest

from agentprint import fix
from agentprint.fixreport import describe, rows
from agentprint.scanner import scan
from agentprint.ui import fixview
from tests import fakes
from tests.fixtures import agent_homes as homes

USER = homes.MAC_CODE


def _ap005(home, project=None):
    return [f for f in scan(home, project) if f.check_id == "AP005"]


def _fix(home, project=None):
    p = fix.plan(scan(home, project))
    return p, fix.apply(p, home)


def test_vscode_user_mcp_env_becomes_env_reference_and_comments_stay(tmp_path):
    home = tmp_path / "home"
    servers = {"github": homes.stdio("GITHUB_TOKEN", fakes.GITHUB), "x": {"env": {"A": "b"}}}
    text = "{\n  // my servers\n  " + json.dumps({"servers": servers})[1:-1] + ",\n}\n"
    path = homes.write(home / USER / "mcp.json", text)
    path.chmod(0o640)

    p, outcome = _fix(home)

    [change] = [c for c in p.changes if c.kind == "env"]
    assert change.reference == "${env:GITHUB_TOKEN}" and change.var == "GITHUB_TOKEN"
    assert len(outcome.applied) == 1 and not outcome.skipped
    after = path.read_text()
    assert "// my servers" in after and fakes.GITHUB not in after
    assert after == text.replace(f'"{fakes.GITHUB}"', '"${env:GITHUB_TOKEN}"')
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert any(
        "set GITHUB_TOKEN in the environment VS Code starts from" in m.text for m in p.manual
    )
    assert _ap005(home) == []
    [row] = rows(p.changes, p.findings)
    assert describe(row) == 'env.GITHUB_TOKEN of MCP server "github" set to "${env:GITHUB_TOKEN}"'
    assert "${env:GITHUB_TOKEN}" in fixview._what(row).plain
    assert fakes.GITHUB not in outcome.journal.read_text()


def test_workspace_vscode_mcp_json_too(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    doc = {"servers": {"aws": homes.stdio("AWS_ACCESS_KEY_ID", fakes.AWS)}}
    path = homes.write(project / ".vscode" / "mcp.json", json.dumps(doc))
    _fix(home, project)
    after = json.loads(path.read_text())
    assert after["servers"]["aws"]["env"]["AWS_ACCESS_KEY_ID"] == "${env:AWS_ACCESS_KEY_ID}"


def test_qwen_code_settings_env_becomes_reference(tmp_path):
    home = tmp_path / "home"
    homes.qwen(home)
    path = home / ".qwen" / "settings.json"
    p, outcome = _fix(home)
    assert [c.reference for c in p.changes if c.kind == "env"] == ["${GITHUB_TOKEN}"]
    after = json.loads(path.read_text())
    assert after["mcpServers"]["github"]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
    assert after["env"]["OPENAI_API_KEY"] == fakes.OPENAI  # not an MCP entry: the human's
    assert any("the shell that starts Qwen Code" in m.text for m in p.manual)


def test_amp_settings_env_becomes_reference_and_headers_stay(tmp_path):
    home = tmp_path / "home"
    homes.amp(home)
    path = home / ".config" / "amp" / "settings.json"
    p, _ = _fix(home)
    after = json.loads(path.read_text())
    servers = after["amp.mcpServers"]
    assert servers["linear"]["env"]["LINEAR_API_KEY"] == "${LINEAR_API_KEY}"
    assert servers["api"]["headers"]["Authorization"] == f"Bearer {fakes.OPENAI}"
    [left] = _ap005(home)
    assert "headers" in left.detail


@pytest.mark.parametrize(
    "build",
    [
        homes.claude_desktop,  # no env reference documented
        homes.cline,
        homes.kiro,  # ${VAR} only after the human approves the variable
        homes.junie,
    ],
)
def test_agents_without_a_documented_reference_are_left_to_the_human(tmp_path, build):
    home = tmp_path / "home"
    build(home)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    findings = _ap005(home)
    assert findings
    p = fix.plan(findings)
    assert [c for c in p.changes if c.kind == "env"] == []
    assert len([m for m in p.manual if m.check_id == "AP005"]) >= len(findings)
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "rel", [Path(".copilot") / "mcp-config.json", Path("proj") / ".github" / "mcp.json"]
)
def test_copilot_cli_and_github_mcp_files_are_not_vscode_format(tmp_path, rel):
    home = tmp_path / "home"
    doc = {"mcpServers": {"gh": homes.stdio("GITHUB_TOKEN", fakes.GITHUB)}}
    if rel.parts[0] == "proj":
        homes.write(tmp_path / rel, json.dumps(doc))
        home.mkdir()
        findings = _ap005(home, tmp_path / "proj")
    else:
        homes.write(home / rel, json.dumps(doc))
        findings = _ap005(home)
    assert findings
    assert [c for c in fix.plan(findings).changes if c.kind == "env"] == []


def test_vscode_file_using_mcp_servers_key_is_not_rewritten(tmp_path):
    """A user mcp.json without the VS Code "servers" key: no slot to rewrite, so no change."""
    home = tmp_path / "home"
    doc = {"mcpServers": {"gh": homes.stdio("GITHUB_TOKEN", fakes.GITHUB)}}
    path = homes.write(home / USER / "mcp.json", json.dumps(doc))
    before = path.read_bytes()
    p, _ = _fix(home)
    assert [c for c in p.changes if c.kind == "env"] == []
    assert path.read_bytes() == before


def test_env_reference_of_a_broken_agent_module_is_none(monkeypatch):
    from agentprint.agents.amp import Amp

    def boom(self, path):
        raise RuntimeError("bug")

    monkeypatch.setattr(Amp, "env_reference", boom)
    assert fix.env_reference("amp", Path("settings.json")) is None
    assert fix.env_reference("no-such-agent", Path("settings.json")) is None


def _env_change(home, project=None):
    p, outcome = _fix(home, project)
    changes = [c for c in p.changes if c.kind == "env"]
    return p, outcome, changes


@pytest.mark.parametrize(
    ("rel", "where"),
    [
        (Path(".factory") / "mcp.json", "the shell that starts Droid"),
        (Path(".augment") / "settings.json", "the shell that starts Auggie"),
        (Path(".augment") / "settings.local.json", None),  # project-only name: not in home
    ],
)
def test_factory_and_augment_env_keys_become_references(tmp_path, rel, where):
    home = tmp_path / "home"
    doc = {
        "mcpServers": {
            "gh": homes.stdio("GITHUB_TOKEN", fakes.GITHUB),
            "api": homes.remote(fakes.OPENAI),
        }
    }
    path = homes.write(home / rel, json.dumps(doc, indent=2))
    p, outcome, changes = _env_change(home)
    if where is None:  # ~/.augment/settings.local.json is not a file Augment keeps at home
        assert changes == []
        return
    assert [c.reference for c in changes] == ["${GITHUB_TOKEN}"] and not outcome.skipped
    after = json.loads(path.read_text())
    assert after["mcpServers"]["gh"]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
    assert after["mcpServers"]["api"] == doc["mcpServers"]["api"]  # a header: the human's
    assert any(where in m.text for m in p.manual)
    [left] = _ap005(home)
    assert "headers" in left.detail


def test_augment_project_settings_too(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    doc = {"mcpServers": {"gh": homes.stdio("GITHUB_TOKEN", fakes.GITHUB)}}
    path = homes.write(project / ".augment" / "settings.local.json", json.dumps(doc))
    _env_change(home, project)
    assert json.loads(path.read_text())["mcpServers"]["gh"]["env"]["GITHUB_TOKEN"] == (
        "${GITHUB_TOKEN}"
    )


def test_kilo_environment_key_becomes_env_reference_and_comment_stays(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    homes.kilo(home)
    homes.kilo_project(project)
    path = home / ".config" / "kilo" / "kilo.jsonc"
    before = path.read_text()
    p, outcome, changes = _env_change(home, project)
    [change] = changes
    assert change.reference == "{env:GH_TOKEN}" and not outcome.skipped
    after = path.read_text()
    assert after == before.replace(f'"{fakes.GITHUB}"', '"{env:GH_TOKEN}"')
    assert after.startswith("// my config\n")
    [row] = rows(p.changes, p.findings)
    assert describe(row) == 'environment.GH_TOKEN of MCP server "gh" set to "{env:GH_TOKEN}"'
    assert fixview._what(row).plain == "gh.environment.GH_TOKEN → {env:GH_TOKEN}"
    assert any("the shell that starts Kilo Code" in m.text for m in p.manual)
    # The project's remote server keeps its header key: the human's, with Kilo's own advice.
    [left] = _ap005(home, project)
    assert left.location == str(project / "kilo.jsonc") and "{env:" in left.fix


def test_kilo_old_extension_file_is_left_to_the_human(tmp_path):
    home = tmp_path / "home"
    settings = homes.MAC_CODE / "globalStorage" / "kilocode.kilo-code" / "settings"
    doc = {"mcpServers": {"gh": homes.stdio("GITHUB_TOKEN", fakes.GITHUB)}}
    path = homes.write(home / settings / "cline_mcp_settings.json", json.dumps(doc))
    before = path.read_bytes()
    p, _, changes = _env_change(home)
    assert changes == [] and path.read_bytes() == before
    assert any("kilo.jsonc" in m.text for m in p.manual if m.check_id == "AP005")


def test_crush_json_env_key_becomes_dollar_name(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    homes.crush_project(project)
    path = project / "crush.json"
    p, outcome, changes = _env_change(home, project)
    assert [c.reference for c in changes] == ["$GITHUB_TOKEN"] and not outcome.skipped
    assert json.loads(path.read_text())["mcp"]["gh"]["env"]["GITHUB_TOKEN"] == "$GITHUB_TOKEN"
    assert any("the shell that starts Crush" in m.text for m in p.manual)
    rc = homes.write(home / ".config" / "crush" / "crushrc", f"mcp add gh --env T={fakes.GITHUB}\n")
    before = rc.read_bytes()
    _env_change(home)
    assert rc.read_bytes() == before  # crushrc is not JSON: never rewritten
