from pathlib import Path

from agentprint.agents import ALL_AGENTS
from agentprint.agents.cursor import Cursor
from agentprint.models import Artifact, HookCommand, Kind
from agentprint.scanner import scan

APP = "Library/Application Support/Cursor/User"


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix()) for a in artifacts}


def _config(path):
    return Cursor().read_config(Artifact("cursor", Kind.SETTINGS, path))


def test_cursor_home_files(agents_home):
    assert _rel(Cursor().discover_home(agents_home), agents_home) == {
        (Kind.SETTINGS, ".cursor/mcp.json"),
        (Kind.SETTINGS, ".cursor/cli-config.json"),
        (Kind.SETTINGS, ".cursor/hooks.json"),
        (Kind.SETTINGS, f"{APP}/settings.json"),
        (Kind.HISTORY, ".cursor/projects/Users-demo-app/agent-transcripts/0001/0001.jsonl"),
        (Kind.HISTORY, f"{APP}/globalStorage/state.vscdb"),  # the .backup copy is skipped
    }


def test_cursor_project_files(agents_project):
    assert _rel(Cursor().discover_project(agents_project), agents_project) == {
        (Kind.SETTINGS, ".cursor/mcp.json")
    }


def test_cursor_cli_permissions_are_grants(agents_home):
    config = _config(agents_home / ".cursor" / "cli-config.json")
    assert [(g.setting, g.value) for g in config.grants] == [
        ("permissions.allow", "Shell(*)"),
        ("approvalMode", "unrestricted"),
    ]


def test_cursor_mcp_wildcard_and_shell_runner_are_grants(tmp_path):
    f = tmp_path / "cli.json"
    f.write_text('{"permissions": {"allow": ["Mcp(*:*)", "Shell(bash)", "Mcp(gh:get_issue)"]}}')
    assert [g.value for g in _config(f).grants] == ["Mcp(*:*)", "Shell(bash)"]


def test_cursor_hooks_and_mcp_servers(agents_home, agents_project):
    assert _config(agents_home / ".cursor" / "hooks.json").hooks == (
        HookCommand("stop", "curl -s -X POST https://hooks.example.invalid/done"),
    )
    [server] = _config(agents_project / ".cursor" / "mcp.json").mcp_servers
    assert server.name == "api" and "${env:<VAR>}" in server.secret_fix
    assert _config(agents_home / APP / "settings.json").grants == ()


def test_cursor_package_rules():
    def matches(name):
        return [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(name)]

    assert matches("package/.cursor/projects/x/agent-transcripts/1/1.jsonl") == ["cursor"]
    assert matches("package/.cursor/chats/abc/def/store.db") == ["cursor"]
    assert matches("package/.cursor/cli-config.json") == ["cursor"]
    assert matches("pkg-1.0/User/globalStorage/state.vscdb") == ["cursor"]
    assert matches("package/.cursor/mcp.json") == ["cursor"]
    assert matches("package/.cursor/rules/style.mdc") == []
    assert matches("package/.cursor/hooks.json") == []
    assert matches("package/src/cursor.ts") == []
    assert matches("package/docs/state.vscdb.md") == []


def test_cursor_history_keys_are_found_in_transcripts_and_state_db(agents_home):
    findings = scan(agents_home, None)
    found = {
        Path(f.location).name for f in findings if (f.check_id, f.agent) == ("AP001", "cursor")
    }
    assert found == {"0001.jsonl", "state.vscdb"}
