"""`agentprint status` sees agentprint installed as an agent's own plugin, and says to keep one
when it is in both as a plugin and through `agentprint install`."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint import plugins
from agentprint.cli import app
from agentprint.integrations import ALL_INTEGRATIONS

runner = CliRunner()

# Where each agent installs a plugin named agentprint (from its docs, see agentprint/plugins.py).
FOLDERS = {
    "claude-code": ".claude/plugins/cache/agentprint/agentprint/0.1.1",
    "codex": ".codex/plugins/cache/agentprint/agentprint/local",
    "copilot": ".copilot/installed-plugins/agentprint/agentprint",
    "gemini": ".gemini/extensions/agentprint",
    "cursor": ".cursor/plugins/local/agentprint",
    "antigravity": ".gemini/config/plugins/agentprint",
    "hermes": ".hermes/plugins/agentprint",
}


def _armed(home: Path, *agents: str) -> None:
    state = home / ".agentprint" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    files = {f"/x/{a}": {"agent": a} for a in agents}
    state.write_text(json.dumps({"version": 1, "files": files, "dirs": []}))


def test_every_plugin_ecosystem_has_a_folder_and_a_way_out():
    assert set(plugins.PLUGIN_FOLDERS) == set(FOLDERS) == set(plugins.REMOVE_PLUGIN)
    assert set(FOLDERS) <= set(ALL_INTEGRATIONS)


@pytest.mark.parametrize("agent", sorted(FOLDERS))
def test_plugin_found_in_its_documented_folder(agent, tmp_path):
    assert not plugins.plugin_found(agent, tmp_path, {})
    (tmp_path / FOLDERS[agent]).mkdir(parents=True)
    assert plugins.plugin_found(agent, tmp_path, {})


@pytest.mark.parametrize("agent", sorted(FOLDERS))
def test_another_plugin_is_not_agentprint(agent, tmp_path):
    (tmp_path / FOLDERS[agent].replace("agentprint", "other-tool")).mkdir(parents=True)
    assert not plugins.plugin_found(agent, tmp_path, {})


def test_agents_without_plugins_are_never_plugin_found(tmp_path):
    for agent in set(ALL_INTEGRATIONS) - set(FOLDERS):
        assert not plugins.plugin_found(agent, tmp_path, {})


def test_moved_config_folders_are_followed(tmp_path):
    (tmp_path / "cc" / "plugins" / "cache" / "m" / "agentprint").mkdir(parents=True)
    assert plugins.plugin_found(
        "claude-code", tmp_path, {"CLAUDE_CONFIG_DIR": str(tmp_path / "cc")}
    )
    (tmp_path / "cx" / "plugins" / "cache" / "m" / "agentprint").mkdir(parents=True)
    assert plugins.plugin_found("codex", tmp_path, {"CODEX_HOME": str(tmp_path / "cx")})


def test_copilot_direct_install_is_found_by_its_manifest_name(tmp_path):
    direct = tmp_path / ".copilot" / "installed-plugins" / "_direct"
    mine = direct / "github-abc123" / ".github" / "plugin"
    mine.mkdir(parents=True)
    (mine / "plugin.json").write_text(json.dumps({"name": "other"}))
    assert not plugins.plugin_found("copilot", tmp_path, {})
    (mine / "plugin.json").write_text(json.dumps({"name": "agentprint"}))
    assert plugins.plugin_found("copilot", tmp_path, {})
    (mine / "plugin.json").write_text("not json")
    assert not plugins.plugin_found("copilot", tmp_path, {})


def test_status_says_armed_twice_and_how_to_keep_one(tmp_path):
    (tmp_path / FOLDERS["claude-code"]).mkdir(parents=True)
    (tmp_path / ".cursor").mkdir()
    _armed(tmp_path, "claude-code")
    out = runner.invoke(app, ["status", "--home", str(tmp_path), "--format", "agent"]).output
    lines = out.splitlines()
    assert "claude-code armed twice (plugin and install)" in lines
    assert "cursor unarmed" in lines
    assert lines[-1] == (
        "Claude Code has agentprint twice, as a plugin and through agentprint install: keep one. "
        "To keep the plugin, run: agentprint uninstall --agent claude-code. To keep install, "
        "remove the plugin: claude plugin uninstall agentprint@agentprint."
    )


def test_status_counts_a_plugin_as_armed(tmp_path):
    (tmp_path / FOLDERS["codex"]).mkdir(parents=True)
    out = runner.invoke(app, ["status", "--home", str(tmp_path), "--format", "agent"]).output
    assert "codex armed (plugin)" in out.splitlines()
    assert "Arm unarmed agents" not in out  # install would arm it a second time
    assert "keep one" not in out
    table = runner.invoke(app, ["status", "--home", str(tmp_path), "--plain"]).output
    assert "1 of 1 agent armed" in table and "Codex         armed (plugin)" in table


def test_status_json_marks_plugins_only_where_found(tmp_path):
    (tmp_path / FOLDERS["gemini"]).mkdir(parents=True)
    _armed(tmp_path, "gemini")
    r = runner.invoke(app, ["status", "--home", str(tmp_path), "--format", "json"])
    by = {a["name"]: a for a in json.loads(r.output)["agents"]}
    assert by["gemini"] == {
        "name": "gemini",
        "label": "Gemini CLI",
        "found": True,
        "armed": True,
        "plugin": True,
    }
    assert "plugin" not in by["codex"]


def test_status_table_wraps_the_keep_one_note(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "60")
    (tmp_path / FOLDERS["hermes"]).mkdir(parents=True)
    (tmp_path / ".hermes" / "config.yaml").write_text("model: x\n")
    _armed(tmp_path, "hermes")
    r = runner.invoke(app, ["status", "--home", str(tmp_path), "--format", "table"])
    assert all(len(line) <= 60 for line in r.output.splitlines()), r.output
    assert "Hermes        armed twice (plugin and install)" in r.output
    assert "hermes plugins disable agentprint" in " ".join(r.output.split())
