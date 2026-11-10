"""`agentprint status` and the agentprint plugin: a plugin arms an agent only when the agent's
own records say it is installed and enabled. A plugin folder whose enable switch agentprint can't
read is "not checked", never counted as armed, and never makes status say "armed twice"."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import agentprint.roster as roster_module
from agentprint import plugins
from agentprint.cli import app
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.plugins import DISABLED, ENABLED, UNKNOWN, plugin_state

runner = CliRunner()
UNKNOWN_WORD = "(agentprint plugin found, not checked whether it is enabled)"
DISABLED_WORD = "(agentprint plugin installed, not enabled)"
# Agents whose enable switch is not documented: a folder there is all agentprint can see.
FOLDER_ONLY = {
    "copilot": ".copilot/installed-plugins/agentprint/agentprint",
    "gemini": ".gemini/extensions/agentprint",
    "cursor": ".cursor/plugins/local/agentprint",
    "antigravity": ".gemini/config/plugins/agentprint",
}


@pytest.fixture(autouse=True)
def _hooked_platform_roster(monkeypatch):
    """Pin the roster where install adds hooks; Windows has its own tests (test_windows_gate)."""
    monkeypatch.setattr(roster_module, "platform", SimpleNamespace(is_windows=lambda: False))


def _armed(home: Path, *agents: str) -> None:
    state = home / ".agentprint" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    files = {f"/x/{a}": {"agent": a} for a in agents}
    state.write_text(json.dumps({"version": 1, "files": files, "dirs": []}))


def _claude(home, *, enabled=None, scope="user", orphaned=False, record=True, market="agentprint"):
    """A Claude Code plugin install as the docs describe it: the cached version folder, its
    installed_plugins.json record, and `enabledPlugins` in ~/.claude/settings.json."""
    version = home / ".claude" / "plugins" / "cache" / market / "agentprint" / "0.1.1"
    version.mkdir(parents=True)
    if orphaned:
        (version / ".orphaned_at").write_text("1762500000000")
    if record:
        entry = {"scope": scope, "installPath": str(version), "version": "0.1.1"}
        data = {"version": 2, "plugins": {f"agentprint@{market}": [entry]}}
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(data))
    if enabled is not None:
        settings = {"enabledPlugins": {f"agentprint@{market}": enabled}}
        (home / ".claude" / "settings.json").write_text(json.dumps(settings))
    return version


def _codex(home, enabled=None, config=None):
    cache = home / ".codex" / "plugins" / "cache" / "agentprint" / "agentprint" / "local"
    cache.mkdir(parents=True)
    if config is None and enabled is not None:
        config = f'[plugins."agentprint@agentprint"]\nenabled = {str(enabled).lower()}\n'
    if config is not None:
        (home / ".codex" / "config.toml").write_text(config)


def _hermes(home, config: str | None):
    (home / ".hermes" / "plugins" / "agentprint").mkdir(parents=True)
    if config is not None:
        (home / ".hermes" / "config.yaml").write_text(config)


def _status(home, fmt):
    result = runner.invoke(app, ["status", "--home", str(home), "--format", fmt, "--plain"])
    assert result.exit_code == 0, result.output
    return result.output


def test_every_plugin_ecosystem_has_a_state_and_a_way_out():
    assert set(plugins.PLUGIN_STATE) == set(plugins.REMOVE_PLUGIN)
    assert set(plugins.PLUGIN_STATE) <= set(ALL_INTEGRATIONS)
    for agent in set(ALL_INTEGRATIONS) - set(plugins.PLUGIN_STATE):
        assert plugin_state(agent, Path("/nonexistent"), {}) is None


# ---- Claude Code -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "state"), [(None, ENABLED), (True, ENABLED), (False, DISABLED)]
)
def test_claude_reads_installed_plugins_and_enabled_plugins(tmp_path, enabled, state):
    _claude(tmp_path, enabled=enabled)
    assert plugin_state("claude-code", tmp_path, {}) == state


def test_claude_orphaned_version_is_not_a_plugin(tmp_path):
    _claude(tmp_path, orphaned=True)
    assert plugin_state("claude-code", tmp_path, {}) is None


def test_claude_stale_cache_after_uninstall_is_not_a_plugin(tmp_path):
    # The reproduction: an orphaned cached version, the plugin turned off, no install record.
    _claude(tmp_path, enabled=False, orphaned=True, record=False)
    record = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    for text in ("{}", json.dumps({"version": 2, "plugins": {}})):
        record.write_text(text)
        assert plugin_state("claude-code", tmp_path, {}) is None


def test_claude_live_cache_but_no_readable_record_is_unknown(tmp_path):
    _claude(tmp_path, record=False)
    assert plugin_state("claude-code", tmp_path, {}) == UNKNOWN
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text("{not json")
    assert plugin_state("claude-code", tmp_path, {}) == UNKNOWN


def test_claude_unreadable_settings_is_unknown(tmp_path):
    _claude(tmp_path)
    (tmp_path / ".claude" / "settings.json").write_text("{not json")
    assert plugin_state("claude-code", tmp_path, {}) == UNKNOWN


def test_claude_project_install_is_unknown_here(tmp_path):
    _claude(tmp_path, scope="project")
    assert plugin_state("claude-code", tmp_path, {}) == UNKNOWN


def test_claude_another_plugin_is_not_agentprint(tmp_path):
    other = tmp_path / ".claude" / "plugins" / "cache" / "m" / "other-tool" / "1.0.0"
    other.mkdir(parents=True)
    entry = {"scope": "user", "installPath": str(other)}
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"other-tool@m": [entry]}})
    )
    assert plugin_state("claude-code", tmp_path, {}) is None


def test_claude_moved_folders_are_followed(tmp_path):
    moved = tmp_path / "cc"
    _claude(moved, record=False)  # under moved/.claude
    env = {"CLAUDE_CONFIG_DIR": str(moved / ".claude")}
    assert plugin_state("claude-code", tmp_path, env) == UNKNOWN
    cache = {"CLAUDE_CODE_PLUGIN_CACHE_DIR": str(moved / ".claude" / "plugins")}
    assert plugin_state("claude-code", tmp_path, cache) == UNKNOWN
    assert plugin_state("claude-code", tmp_path, {}) is None


# ---- Codex -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("enabled", "state"), [(True, ENABLED), (False, DISABLED)])
def test_codex_reads_the_plugin_table_in_config_toml(tmp_path, enabled, state):
    _codex(tmp_path, enabled)
    assert plugin_state("codex", tmp_path, {}) == state


@pytest.mark.parametrize("config", [None, 'model = "x"\n', "[plugins\nbroken"])
def test_codex_without_a_readable_switch_is_unknown(tmp_path, config):
    _codex(tmp_path, config=config)
    assert plugin_state("codex", tmp_path, {}) == UNKNOWN


def test_codex_without_a_cached_plugin_is_none(tmp_path):
    (tmp_path / ".codex").mkdir()
    switch = '[plugins."agentprint@agentprint"]\nenabled = true\n'
    (tmp_path / ".codex" / "config.toml").write_text(switch)
    assert plugin_state("codex", tmp_path, {}) is None
    _codex(tmp_path / "moved", True)
    env = {"CODEX_HOME": str(tmp_path / "moved" / ".codex")}
    assert plugin_state("codex", tmp_path, env) == ENABLED


# ---- Hermes Agent ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "state"),
    [
        ("plugins:\n  enabled: [agentprint]\n", ENABLED),
        ("plugins:\n  enabled: []\n", DISABLED),  # plugins are opt-in
        ("model: x\n", DISABLED),
        ("plugins:\n  enabled: [agentprint]\n  disabled: [agentprint]\n", DISABLED),
        (None, UNKNOWN),
        ("plugins: [\n", UNKNOWN),
        ("plugins:\n  enabled: agentprint\n", UNKNOWN),
    ],
)
def test_hermes_reads_plugins_enabled(tmp_path, config, state):
    _hermes(tmp_path, config)
    assert plugin_state("hermes", tmp_path, {}) == state


def test_hermes_without_the_plugin_folder_is_none(tmp_path):
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text("plugins:\n  enabled: [agentprint]\n")
    assert plugin_state("hermes", tmp_path, {}) is None


# ---- agents with no readable switch --------------------------------------------------------------


@pytest.mark.parametrize("agent", sorted(FOLDER_ONLY))
def test_a_plugin_folder_alone_is_unknown(agent, tmp_path):
    assert plugin_state(agent, tmp_path, {}) is None
    (tmp_path / FOLDER_ONLY[agent].replace("agentprint", "other-tool")).mkdir(parents=True)
    assert plugin_state(agent, tmp_path, {}) is None
    (tmp_path / FOLDER_ONLY[agent]).mkdir(parents=True)
    assert plugin_state(agent, tmp_path, {}) == UNKNOWN


def test_copilot_direct_install_is_found_by_its_manifest_name(tmp_path):
    mine = tmp_path / ".copilot" / "installed-plugins" / "_direct" / "github-abc123"
    (mine / ".github" / "plugin").mkdir(parents=True)
    manifest = mine / ".github" / "plugin" / "plugin.json"
    for text, state in [('{"name": "other"}', None), ('{"name": "agentprint"}', UNKNOWN)]:
        manifest.write_text(text)
        assert plugin_state("copilot", tmp_path, {}) == state
    manifest.write_text("not json")
    assert plugin_state("copilot", tmp_path, {}) is None


# ---- what status says ----------------------------------------------------------------------------


def test_enabled_plugin_and_install_is_armed_twice_with_a_way_to_keep_one(tmp_path):
    _claude(tmp_path, enabled=True)
    (tmp_path / ".cursor").mkdir()
    _armed(tmp_path, "claude-code")
    lines = _status(tmp_path, "agent").splitlines()
    assert "claude-code armed twice (plugin and install)" in lines
    assert lines[-1] == (
        "Claude Code has agentprint twice, as a plugin and through agentprint install: keep one. "
        "To keep the plugin, run: agentprint uninstall --agent claude-code. To keep install, "
        "remove the plugin: claude plugin uninstall agentprint@agentprint."
    )
    by = {a["name"]: a for a in json.loads(_status(tmp_path, "json"))["agents"]}
    assert by["claude-code"]["plugin"] == "enabled" and by["claude-code"]["armed"] is True
    assert "plugin" not in by["cursor"]
    assert "Claude Code   armed twice (plugin and install)" in _status(tmp_path, "table")


def test_enabled_plugin_alone_counts_as_armed(tmp_path):
    _codex(tmp_path, True)
    out = _status(tmp_path, "agent")
    assert "codex armed (plugin)" in out.splitlines()
    assert "Arm unarmed agents" not in out and "keep one" not in out
    table = _status(tmp_path, "table")
    assert "1 of 1 agent armed" in table and "Codex         armed (plugin)" in table


def test_orphaned_and_not_enabled_plugins_never_arm(tmp_path):
    """The review's reproduction: Claude Code's stale cache with the plugin turned off and no
    install record, and a Hermes plugin folder that is not enabled."""
    _claude(tmp_path, enabled=False, orphaned=True, record=False)
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text("{}")
    _hermes(tmp_path, "plugins:\n  enabled: []\n")
    lines = _status(tmp_path, "agent").splitlines()
    assert "claude-code unarmed" in lines
    assert f"hermes unarmed {DISABLED_WORD}" in lines
    assert "Arm unarmed agents with: agentprint install" in lines
    assert not any("twice" in line or "keep one" in line for line in lines)
    by = {a["name"]: a for a in json.loads(_status(tmp_path, "json"))["agents"]}
    assert "plugin" not in by["claude-code"] and by["hermes"]["plugin"] == "disabled"
    table = _status(tmp_path, "table")
    assert "0 of 2 agents armed" in table and "armed (plugin)" not in table


def test_disabled_plugin_beside_install_is_not_twice(tmp_path):
    _claude(tmp_path, enabled=False)
    _armed(tmp_path, "claude-code")
    out = _status(tmp_path, "agent")
    assert f"claude-code armed {DISABLED_WORD}" in out.splitlines()
    assert "twice" not in out and "keep one" not in out


def test_unknown_plugin_is_reported_not_counted_and_install_hint_kept(tmp_path):
    (tmp_path / FOLDER_ONLY["cursor"]).mkdir(parents=True)
    lines = _status(tmp_path, "agent").splitlines()
    assert f"cursor unarmed {UNKNOWN_WORD}" in lines
    assert "Arm unarmed agents with: agentprint install" in lines
    assert not any("twice" in line for line in lines)
    by = {a["name"]: a for a in json.loads(_status(tmp_path, "json"))["agents"]}
    assert by["cursor"]["plugin"] == "unknown" and by["cursor"]["armed"] is False
    assert "0 of 1 agent armed · Cursor is not" in _status(tmp_path, "table")


def test_unknown_plugin_beside_install_says_check_and_keep_one(tmp_path):
    (tmp_path / FOLDER_ONLY["gemini"]).mkdir(parents=True)
    _armed(tmp_path, "gemini")
    lines = _status(tmp_path, "agent").splitlines()
    assert f"gemini armed {UNKNOWN_WORD}" in lines
    assert lines[-1].startswith(
        "Gemini CLI also has the agentprint plugin, and agentprint could not check whether it "
        "is enabled. If it is, keep one."
    )
    assert "gemini extensions uninstall agentprint" in lines[-1]
    assert not any("twice" in line for line in lines)


def test_status_table_wraps_the_keep_one_note(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "60")
    _hermes(tmp_path, "plugins:\n  enabled: [agentprint]\n")
    _armed(tmp_path, "hermes")
    r = runner.invoke(app, ["status", "--home", str(tmp_path), "--format", "table"])
    assert all(len(line) <= 60 for line in r.output.splitlines()), r.output
    assert "Hermes        armed twice (plugin and install)" in r.output
    assert "hermes plugins disable agentprint" in " ".join(r.output.split())
