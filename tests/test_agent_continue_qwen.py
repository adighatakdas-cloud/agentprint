"""Continue and Qwen Code: JSON and JSONL history, YAML and JSON MCP config, key stores."""

from pathlib import Path

import pytest

from agentprint import fix
from agentprint.agents.continue_dev import Continue
from agentprint.agents.qwen_code import QwenCode
from agentprint.models import Artifact, HookCommand, Kind
from agentprint.scanner import scan
from tests import fakes
from tests.fixtures import agent_homes as homes


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix(), a.personal) for a in artifacts}


def _found(findings, agent):
    return {(f.check_id, Path(f.location).name) for f in findings if f.agent == agent}


# --- Continue ---------------------------------------------------------------------------------


def test_continue_home_files(tmp_path):
    home = tmp_path / "home"
    homes.continue_dev(home)
    assert _rel(Continue().discover_home(home), home) == {
        (Kind.SETTINGS, ".continue/config.yaml", False),
        (Kind.SETTINGS, ".continue/mcpServers/search.json", False),
        (Kind.HISTORY, ".continue/sessions/s1.json", False),
        (Kind.HISTORY, ".continue/logs/prompt.log", False),
        (Kind.CREDENTIALS, ".continue/.env", False),
        (Kind.CREDENTIALS, ".continue/auth.json", False),
    }


def test_continue_findings(tmp_path):
    home = tmp_path / "home"
    homes.continue_dev(home)
    found = _found(scan(home, None), "continue")
    assert found - {("AP008", name) for _, name in found} == {
        ("AP001", "s1.json"),
        ("AP001", "prompt.log"),
        ("AP002", "config.yaml"),  # the model's raw apiKey
        ("AP005", "config.yaml"),  # the MCP server's env, from a YAML list
        ("AP005", "search.json"),
    }


def test_continue_yaml_server_list_and_secret_fix(tmp_path):
    home = tmp_path / "home"
    homes.continue_dev(home)
    path = home / ".continue" / "config.yaml"
    [server] = Continue().read_config(Artifact("continue", Kind.SETTINGS, path)).mcp_servers
    assert server.name == "github" and "${{ secrets.<VAR> }}" in server.secret_fix
    finding = next(f for f in scan(home, None) if f.check_id == "AP005" and f.location == str(path))
    assert "${{ secrets.GITHUB_PERSONAL_ACCESS_TOKEN }}" in finding.fix


def test_continue_project_files(tmp_path):
    root = tmp_path / "proj"
    homes.write(root / ".continue" / "mcpServers" / "db.yaml", "name: db\nmcpServers: []\n")
    homes.write(root / ".continue" / ".env", "X=1\n")
    homes.write(root / ".continue" / "settings.local.json", "{}")
    homes.write(root / ".continue" / "rules" / "style.md", "Use tabs.\n")  # instructions
    assert _rel(Continue().discover_project(root), root) == {
        (Kind.SETTINGS, ".continue/mcpServers/db.yaml", False),
        (Kind.CREDENTIALS, ".continue/.env", True),
        (Kind.SETTINGS, ".continue/settings.local.json", True),
    }


def test_continue_records():
    c = Continue()
    assert c.pure_history(Path("/u/.continue/sessions/s1.json"))
    assert c.pure_history(Path("/u/.continue/logs/prompt.log"))
    assert c.pure_history(Path("/u/.continue/dev_data/0.2.0/chat.jsonl"))
    assert not c.pure_history(Path("/u/.continue/config.yaml"))
    assert not c.pure_history(Path("/u/.continue/rules/style.md"))


# --- Qwen Code --------------------------------------------------------------------------------


def test_qwen_home_files(tmp_path):
    home = tmp_path / "home"
    homes.qwen(home)
    assert _rel(QwenCode().discover_home(home), home) == {
        (Kind.SETTINGS, ".qwen/settings.json", False),
        (Kind.HISTORY, ".qwen/projects/-Users-demo-app/chats/s1.jsonl", False),
        (Kind.HISTORY, ".qwen/projects/-Users-demo-app/memory/notes.md", False),
        (Kind.HISTORY, ".qwen/tmp/0f3a9c/shell_history", False),
        (Kind.CREDENTIALS, ".qwen/oauth_creds.json", False),
        (Kind.CREDENTIALS, ".qwen/.env", False),
    }


def test_qwen_findings_and_config(tmp_path):
    home = tmp_path / "home"
    homes.qwen(home)
    found = _found(scan(home, None), "qwen-code")
    assert found - {("AP008", name) for _, name in found} == {
        ("AP001", "s1.jsonl"),
        ("AP001", "shell_history"),
        ("AP002", "settings.json"),  # the plain-text env block
        ("AP004", "settings.json"),
        ("AP005", "settings.json"),
    }
    config = QwenCode().read_config(
        Artifact("qwen-code", Kind.SETTINGS, home / ".qwen" / "settings.json")
    )
    assert config.hooks == (HookCommand("PreToolUse", "curl -s https://hooks.example.invalid/x"),)
    assert [s.name for s in config.mcp_servers] == ["github"]


@pytest.mark.parametrize(
    ("rel", "record"),
    [
        (".qwen/projects/p/chats/s.jsonl", True),
        (".qwen/tmp/abc/shell_history", True),
        (".qwen/tmp/abc/chats/s.json", True),
        (".qwen/file-history/x/a.py", True),
        (".qwen/projects/p/memory/notes.md", False),  # read back every session
        (".qwen/QWEN.md", False),
        (".qwen/settings.json", False),
    ],
)
def test_qwen_records(rel, record):
    assert QwenCode().pure_history(Path("/u") / rel) is record


def test_qwen_project_files(tmp_path):
    root = tmp_path / "proj"
    for rel in (".qwen/settings.json", ".qwen/.env", ".qwen/QWEN.local.md", "QWEN.md"):
        homes.write(root / rel, "{}")
    assert _rel(QwenCode().discover_project(root), root) == {
        (Kind.SETTINGS, ".qwen/settings.json", False),
        (Kind.CREDENTIALS, ".qwen/.env", True),
        (Kind.PERSONAL, ".qwen/QWEN.local.md", True),
    }


def test_qwen_memory_key_is_left_to_the_human(tmp_path):
    home = tmp_path / "home"
    memory = homes.write(
        home / ".qwen" / "projects" / "p" / "memory" / "m.md", f"deploy key {fakes.GITHUB}\n"
    )
    p = fix.plan(scan(home, None))
    assert p.changes == []
    assert any("reads that file back" in m.text for m in p.manual)
    assert fakes.GITHUB in memory.read_text()


def test_continue_legacy_config_json_servers(tmp_path):
    import json

    path = tmp_path / ".continue" / "config.json"
    transport = {"type": "stdio", "command": "uvx", "args": ["gh-mcp"], "env": {"GH": fakes.GITHUB}}
    legacy = {"experimental": {"modelContextProtocolServers": [{"transport": transport}, "x", {}]}}
    homes.write(path, json.dumps(legacy))
    [server] = Continue().read_config(Artifact("continue", Kind.SETTINGS, path)).mcp_servers
    assert server.name == "#1" and dict(server.fields)["env.GH"] == f"GH={fakes.GITHUB}"
    [finding] = [f for f in scan(tmp_path, None) if f.check_id == "AP005"]
    assert finding.agent == "continue" and "'#1'" in finding.detail
