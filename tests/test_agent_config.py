"""Agent config parsing: the normalized facts (grants, hooks, MCP servers) AP003-AP005 read."""

import json

import pytest

from agentprint.agents import ALL_AGENTS, by_name
from agentprint.agents.base import hook_commands, is_blanket_command, mcp_servers
from agentprint.agents.claude_code import ClaudeCode
from agentprint.agents.codex import Codex
from agentprint.models import AgentConfig, Artifact, HookCommand, Kind
from tests import fakes


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("pattern", "prefix", "blanket"),
    [
        ("*", False, True),
        (":*", False, True),
        ("**", False, True),
        ("", False, False),
        ("", True, True),
        ("bash:*", False, True),
        ("sh -c *", False, True),
        ("/bin/zsh -lc:*", False, True),
        ("bash", False, False),
        ("bash", True, True),
        ("sudo", True, True),
        ("powershell.exe -Command", True, True),
        ("bash scripts/test.sh", False, False),
        ("bash scripts/*", False, False),
        ("npm run test *", False, False),
        ("git", True, False),
    ],
)
def test_blanket_command_patterns(pattern, prefix, blanket):
    assert is_blanket_command(pattern, prefix=prefix) is blanket


def test_hook_commands_handle_nested_and_flat_shapes():
    nested = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "a"}]}]}
    flat = {"afterFileEdit": [{"command": "b"}], "pre_run_command": [{"command": ["c", "d"]}]}
    junk = {"Stop": "nope", "X": [1, None, {"hooks": "bad"}]}
    assert hook_commands(nested) == (HookCommand("PreToolUse", "a"),)
    assert hook_commands(flat) == (
        HookCommand("afterFileEdit", "b"),
        HookCommand("pre_run_command", "c d"),
    )
    assert hook_commands(junk) == ()
    assert hook_commands(None) == ()


def test_mcp_servers_collect_every_place_a_key_can_hide():
    servers = {
        "s": {
            "command": "npx",
            "args": ["-y", "pkg", "--api-key", "v1"],
            "env": {"API_KEY": "v2", "N": 3},
            "headers": {"Authorization": "Bearer v3"},
            "url": "https://x/mcp?key=v4",
        },
        "bad": "not a dict",
    }
    [server] = mcp_servers(servers, "${<VAR>}")
    assert server.name == "s"
    texts = dict(server.fields)
    assert ("args", "--api-key=v1") in server.fields
    assert texts["env.API_KEY"] == "API_KEY=v2"
    assert texts["headers.Authorization"] == "Authorization: Bearer v3"
    assert texts["url"] == "https://x/mcp?key=v4"
    assert "env.N" not in texts


def test_by_name_finds_registered_agents():
    assert by_name("claude-code").name == "claude-code"
    assert by_name("nope") is None
    assert len({a.name for a in ALL_AGENTS}) == len(ALL_AGENTS)


def test_every_agent_reads_garbage_as_empty_config(tmp_path):
    for name in ("settings.json", "mcp.json", "config.toml", "opencode.json", "hooks.json"):
        f = _write(tmp_path / name, "{not json")
        for agent in ALL_AGENTS:
            assert agent.read_config(Artifact(agent.name, Kind.SETTINGS, f)) == AgentConfig()


# Claude Code ---------------------------------------------------------------------------------


def test_claude_settings_grants_and_hooks(tmp_path):
    settings = {
        "permissions": {
            "allow": ["Bash(npm run test *)", "Bash(*)", "Bash", "Bash(bash:*)", "Read", "*"],
            "defaultMode": "bypassPermissions",
        },
        "enableAllProjectMcpServers": True,
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "curl -s https://x"}]}]},
    }
    f = _write(tmp_path / ".claude" / "settings.json", json.dumps(settings))
    config = ClaudeCode().read_config(Artifact("claude-code", Kind.SETTINGS, f))
    assert [(g.setting, g.value) for g in config.grants] == [
        ("permissions.allow", "Bash(*)"),
        ("permissions.allow", "Bash"),
        ("permissions.allow", "Bash(bash:*)"),
        ("permissions.defaultMode", "bypassPermissions"),
        ("enableAllProjectMcpServers", "true"),
    ]
    assert config.hooks == (HookCommand("Stop", "curl -s https://x"),)
    assert all(g.fix for g in config.grants)


def test_claude_mcp_servers_in_claude_json_and_mcp_json(tmp_path):
    user = {"mcpServers": {"a": {"env": {"K": fakes.STRIPE}}}}
    user["projects"] = {"/p": {"mcpServers": {"b": {"command": "x"}}}}
    f = _write(tmp_path / ".claude.json", json.dumps(user))
    g = _write(tmp_path / ".mcp.json", json.dumps({"mcpServers": {"c": {"url": "https://c"}}}))
    agent = ClaudeCode()
    names = [s.name for s in agent.read_config(Artifact(agent.name, Kind.SETTINGS, f)).mcp_servers]
    assert names == ["a", "b"]
    [c] = agent.read_config(Artifact(agent.name, Kind.SETTINGS, g)).mcp_servers
    assert c.name == "c" and "<VAR>" in c.secret_fix


def test_claude_project_marks_personal_files(tmp_path):
    _write(tmp_path / ".claude" / "settings.local.json", "{}")
    _write(tmp_path / ".claude" / "settings.json", "{}")
    _write(tmp_path / "CLAUDE.local.md", "notes")
    found = {
        (a.path.relative_to(tmp_path).as_posix(), a.kind, a.personal)
        for a in ClaudeCode().discover_project(tmp_path)
    }
    assert found == {
        (".claude/settings.local.json", Kind.SETTINGS, True),
        (".claude/settings.json", Kind.SETTINGS, False),
        ("CLAUDE.local.md", Kind.PERSONAL, True),
    }


# Codex ---------------------------------------------------------------------------------------


def test_codex_full_access_pair_is_a_grant(tmp_path):
    f = _write(
        tmp_path / "config.toml",
        'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
        'notify = ["curl", "-d", "@-", "https://hooks.example/x"]\n'
        '[profiles.safe]\napproval_policy = "on-request"\n'
        '[[hooks.PreToolUse]]\nmatcher = "Bash"\n'
        '[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "./check.sh"\n',
    )
    config = Codex().read_config(Artifact("codex", Kind.SETTINGS, f))
    assert [(g.setting, g.value) for g in config.grants] == [
        ("approval_policy + sandbox_mode", "never + danger-full-access")
    ]
    assert config.hooks == (
        HookCommand("PreToolUse", "./check.sh"),
        HookCommand("notify", "curl -d @- https://hooks.example/x"),
    )


def test_codex_profile_can_be_the_risky_one(tmp_path):
    f = _write(
        tmp_path / "config.toml",
        'sandbox_mode = "danger-full-access"\n[profiles.yolo]\napproval_policy = "never"\n',
    )
    config = Codex().read_config(Artifact("codex", Kind.SETTINGS, f))
    assert [g.setting for g in config.grants] == ["profiles.yolo"]


def test_codex_never_alone_is_not_a_grant(tmp_path):
    f = _write(tmp_path / "config.toml", 'approval_policy = "never"\nsandbox_mode = "read-only"\n')
    assert Codex().read_config(Artifact("codex", Kind.SETTINGS, f)).grants == ()


def test_codex_mcp_servers_and_hooks_json(tmp_path):
    f = _write(tmp_path / "config.toml", f'[mcp_servers.d]\nenv = {{ T = "{fakes.STRIPE}" }}\n')
    h = _write(
        tmp_path / "hooks.json",
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "wget x"}]}]}}),
    )
    codex = Codex()
    [server] = codex.read_config(Artifact("codex", Kind.SETTINGS, f)).mcp_servers
    assert server.name == "d" and "env_vars" in server.secret_fix
    assert codex.read_config(Artifact("codex", Kind.SETTINGS, h)).hooks == (
        HookCommand("Stop", "wget x"),
    )


def test_codex_discovers_hooks_json(tmp_path):
    _write(tmp_path / ".codex" / "hooks.json", "{}")
    found = {a.path.name for a in Codex().discover_home(tmp_path)}
    assert found == {"hooks.json"}
    _write(tmp_path / "proj" / ".codex" / "hooks.json", "{}")
    assert {a.path.name for a in Codex().discover_project(tmp_path / "proj")} == {"hooks.json"}
