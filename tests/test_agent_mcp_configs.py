"""One MCP-config reader for every format: VS Code (servers), Claude Desktop, Kiro, Factory
Droid, Junie, Augment (mcpServers), Amp (amp.mcpServers), plus the shapes the reader accepts."""

import json
from pathlib import Path

import pytest

from agentprint.agents import ALL_AGENTS, by_name
from agentprint.agents.amp import Amp
from agentprint.agents.augment import Augment
from agentprint.agents.base import read_mcp_servers
from agentprint.agents.claude_desktop import ClaudeDesktop
from agentprint.agents.factory_droid import FactoryDroid
from agentprint.agents.junie import Junie
from agentprint.agents.kiro import Kiro
from agentprint.models import AgentConfig, Artifact, HookCommand, Kind
from agentprint.scanner import scan
from tests import fakes
from tests.fixtures import agent_homes as homes


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix(), a.personal) for a in artifacts}


def _by_agent(findings, agent):
    return {(f.check_id, Path(f.location).name) for f in findings if f.agent == agent}


def _config(agent, path):
    return agent.read_config(Artifact(agent.name, Kind.SETTINGS, path))


# --- the shared reader ------------------------------------------------------------------------


def test_reader_takes_maps_lists_and_alias_field_names():
    data = {
        "mcpServers": {"a": {"command": "x", "env": {"K": "v1"}}},
        "servers": [{"name": "b", "url": "https://x/mcp", "headers": {"H": "v2"}}, {"no": "name"}],
        "extensions": {"c": {"cmd": "y", "envs": {"K2": "v3"}, "uri": "https://y/mcp"}},
    }
    found = read_mcp_servers(data, "fix", "mcpServers", "servers", "extensions")
    assert [s.name for s in found] == ["a", "b", "c"]
    assert dict(found[2].fields) == {"command": "y", "env.K2": "K2=v3", "url": "https://y/mcp"}
    assert dict(found[1].fields)["headers.H"] == "H: v2"


@pytest.mark.parametrize("data", [None, [], "text", {"mcpServers": "x"}, {"mcpServers": [1, 2]}])
def test_reader_ignores_wrong_shapes(data):
    assert read_mcp_servers(data, "fix", "mcpServers") == ()


# --- Claude Desktop ---------------------------------------------------------------------------


def test_claude_desktop_config_and_logs_on_macos(tmp_path):
    home = tmp_path / "home"
    homes.claude_desktop(home)
    assert _rel(ClaudeDesktop().discover_home(home), home) == {
        (Kind.SETTINGS, "Library/Application Support/Claude/claude_desktop_config.json", False),
        (Kind.HISTORY, "Library/Logs/Claude/mcp-server-github.log", False),
    }
    found = _by_agent(scan(home, None), "claude-desktop")
    assert found - {("AP008", n) for _, n in found} == {
        ("AP005", "claude_desktop_config.json"),
        ("AP011", "claude_desktop_config.json"),  # npx -y with no version
        ("AP001", "mcp-server-github.log"),
    }


def test_claude_desktop_on_windows_and_linux(tmp_path):
    home = tmp_path / "win"
    homes.claude_desktop_windows(home)
    [found] = [f for f in scan(home, None) if f.check_id == "AP005"]
    assert found.agent == "claude-desktop" and "STRIPE_SECRET_KEY" in found.detail
    assert "no environment-variable reference" in found.fix and fakes.STRIPE not in found.fix
    linux = tmp_path / "linux"
    homes.write(linux / ".config" / "Claude" / "logs" / "mcp.log", "x\n")
    homes.write(linux / ".config" / "Claude" / "claude_desktop_config.json", "{}")
    assert {a.kind for a in ClaudeDesktop().discover_home(linux)} == {Kind.SETTINGS, Kind.HISTORY}
    assert ClaudeDesktop().pure_history(linux / ".config" / "Claude" / "logs" / "mcp.log")
    assert not ClaudeDesktop().pure_history(linux / ".config" / "Claude" / "config.json")


def test_claude_desktop_example_configs_in_packages_are_not_flagged():
    def matches(name):
        return [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(name)]

    assert matches("package/claude_desktop_config.json") == []
    assert matches("package/examples/claude_desktop_config.json") == []


# --- VS Code (Copilot module) -----------------------------------------------------------------


def test_vscode_user_and_profile_mcp_json(tmp_path):
    home = tmp_path / "home"
    homes.vscode(home)
    found = [f for f in scan(home, None) if f.check_id == "AP005"]
    assert {(f.agent, Path(f.location).parent.name) for f in found} == {
        ("copilot", "User"),
        ("copilot", "-5f3a1b"),
    }
    # The advice names what fix writes (${env:NAME}) first, and ${input:} as the alternative.
    for f in found:
        assert f.fix.index("${env:") < f.fix.index("${input:")


# --- Kiro -------------------------------------------------------------------------------------


def test_kiro_home_and_project(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    homes.kiro(home)
    homes.kiro_project(project)
    assert _rel(Kiro().discover_home(home), home) == {
        (Kind.SETTINGS, ".kiro/settings/mcp.json", False),
        (Kind.SETTINGS, ".kiro/settings/cli.json", False),
        (Kind.SETTINGS, ".kiro/hooks/notify.json", False),
    }
    assert _rel(Kiro().discover_project(project), project) == {
        (Kind.SETTINGS, ".kiro/settings/mcp.json", False)  # specs and steering are not read
    }
    findings = scan(home, project)
    ap005 = [f for f in findings if f.check_id == "AP005" and f.agent == "kiro"]
    assert len(ap005) == 3 and all("approve <VAR>" not in f.fix for f in ap005)
    assert any("approve GITHUB_PERSONAL_ACCESS_TOKEN" in f.fix for f in ap005)
    assert ("AP004", "notify.json") in _by_agent(findings, "kiro")


def test_kiro_hook_file_shapes(tmp_path):
    hooks = tmp_path / ".kiro" / "hooks"
    off = {"trigger": "Stop", "enabled": False, "action": {"type": "command", "command": "nc x 1"}}
    ask = {"trigger": "Stop", "action": {"type": "askAgent", "prompt": "check"}}
    homes.write(hooks / "a.json", json.dumps({"version": "v1", "hooks": [off, ask, "x"]}))
    assert _config(Kiro(), hooks / "a.json") == AgentConfig()
    homes.write(hooks / "b.json", json.dumps({"hooks": {"not": "a list"}}))
    assert _config(Kiro(), hooks / "b.json") == AgentConfig()
    assert Kiro().pure_history(tmp_path / ".kiro" / "anything") is False


# --- Factory Droid ----------------------------------------------------------------------------


def test_factory_home_project_and_checks(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    homes.factory(home)
    homes.factory_project(project)
    assert _rel(FactoryDroid().discover_home(home), home) == {
        (Kind.SETTINGS, ".factory/settings.json", False),
        (Kind.SETTINGS, ".factory/mcp.json", False),
        (Kind.SETTINGS, ".factory/hooks.json", False),
        (Kind.HISTORY, ".factory/sessions/-Users-demo-app/s1.jsonl", False),
    }
    assert _rel(FactoryDroid().discover_project(project), project) == {
        (Kind.SETTINGS, ".factory/mcp.json", False),
        (Kind.SETTINGS, ".factory/settings.local.json", True),
    }
    found = _by_agent(scan(home, project), "factory-droid")
    assert {
        ("AP002", "settings.json"),  # customModels[].apiKey
        ("AP005", "mcp.json"),
        ("AP004", "hooks.json"),
        ("AP001", "s1.jsonl"),
    } <= found
    hooks = _config(FactoryDroid(), home / ".factory" / "hooks.json").hooks
    assert hooks == (HookCommand("PreToolUse", "wget -qO- https://hooks.example.invalid/x"),)


# --- Augment, Amp, Junie ----------------------------------------------------------------------


def test_augment_settings_and_login_store(tmp_path):
    home = tmp_path / "home"
    homes.augment(home)
    assert _rel(Augment().discover_home(home), home) == {
        (Kind.SETTINGS, ".augment/settings.json", False),
        (Kind.CREDENTIALS, ".augment/session.json", False),
    }
    found = _by_agent(scan(home, None), "augment")
    assert {("AP005", "settings.json"), ("AP004", "settings.json")} <= found
    assert not {c for c, n in found if n == "session.json"} - {"AP008"}


def test_amp_settings_and_unverified_secrets_file(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    homes.amp(home)
    homes.write(project / ".amp" / "settings.jsonc", '{\n  // x\n  "amp.mcpServers": {},\n}\n')
    assert _rel(Amp().discover_home(home), home) == {
        (Kind.SETTINGS, ".config/amp/settings.json", False),
        (Kind.CREDENTIALS, ".local/share/amp/secrets.json", False),
    }
    assert _rel(Amp().discover_project(project), project) == {
        (Kind.SETTINGS, ".amp/settings.jsonc", False)
    }
    servers = _config(Amp(), home / ".config" / "amp" / "settings.json").mcp_servers
    assert [s.name for s in servers] == ["linear", "api"]
    ap005 = [f for f in scan(home, None) if f.check_id == "AP005"]
    assert {f.agent for f in ap005} == {"amp"} and len(ap005) == 2
    # The secrets file (third-party report) is a key store: only who can read it is checked.
    store = [f for f in scan(home, None) if f.location.endswith("secrets.json")]
    assert {f.check_id for f in store} <= {"AP008"}
    # A nested "amp": {"mcpServers": ...} is not Amp's format: nothing is read from it.
    nested = homes.write(tmp_path / "n" / "settings.json", json.dumps({"amp": {"mcpServers": {}}}))
    assert _config(Amp(), nested) == AgentConfig()


def test_junie_sessions_mcp_and_hooks(tmp_path):
    home = tmp_path / "home"
    homes.junie(home)
    assert _rel(Junie().discover_home(home), home) == {
        (Kind.SETTINGS, ".junie/mcp/mcp.json", False),
        (Kind.SETTINGS, ".junie/config.json", False),
        (Kind.HISTORY, ".junie/sessions/s-0001/transcript.md", False),
        (Kind.HISTORY, ".junie/sessions/s-0001/events.jsonl", False),
    }
    found = _by_agent(scan(home, None), "junie")
    assert {("AP001", "transcript.md"), ("AP005", "mcp.json"), ("AP004", "config.json")} <= found
    assert Junie().pure_history(home / ".junie" / "sessions" / "s" / "transcript.md")
    assert not Junie().pure_history(home / ".junie" / "AGENTS.md")


@pytest.mark.parametrize(
    ("member", "agent"),
    [
        ("package/.junie/sessions/s1/transcript.md", "junie"),
        ("package/.junie/mcp/mcp.json", "junie"),
        ("package/.kiro/settings/mcp.json", "kiro"),
        ("package/.factory/mcp.json", "factory-droid"),
        ("package/.augment/session.json", "augment"),
    ],
)
def test_package_rules(member, agent):
    found = [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(member)]
    assert found == [agent]


@pytest.mark.parametrize(
    "member",
    [
        "package/.junie/AGENTS.md",
        "package/.junie/guidelines.md",
        "package/src/junie/sessions.ts",
        "package/.amp/settings.json",
        "package/.augment/settings.json",
        "package/docs/sessions/intro.md",
    ],
)
def test_package_look_alikes_are_not_flagged(member):
    assert [r for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(member)] == []


@pytest.mark.parametrize(
    "name", ["claude-desktop", "kiro", "factory-droid", "augment", "amp", "junie"]
)
def test_every_mcp_agent_has_a_doc_row(name):
    doc = by_name(name).doc
    assert doc.label and doc.reads and doc.fix and doc.checked
