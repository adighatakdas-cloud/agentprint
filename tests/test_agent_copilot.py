import json
from pathlib import Path

from agentprint.agents import ALL_AGENTS
from agentprint.agents.copilot import Copilot
from agentprint.models import Artifact, HookCommand, Kind
from agentprint.scanner import scan

CODE = "Library/Application Support/Code/User"


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix(), a.personal) for a in artifacts}


def _config(path):
    return Copilot().read_config(Artifact("copilot", Kind.SETTINGS, path))


def test_copilot_home_files(more_home):
    assert _rel(Copilot().discover_home(more_home), more_home) == {
        (Kind.SETTINGS, ".copilot/settings.json", False),
        (Kind.SETTINGS, ".copilot/mcp-config.json", False),
        (Kind.SETTINGS, ".copilot/hooks/notify.json", False),
        (Kind.SETTINGS, f"{CODE}/settings.json", False),
        (Kind.HISTORY, ".copilot/session-state/s-0001/events.jsonl", False),
        (Kind.HISTORY, ".copilot/logs/process-1-2.log", False),
        (Kind.HISTORY, f"{CODE}/workspaceStorage/ab12/chatSessions/s1.json", False),
        (Kind.CREDENTIALS, ".copilot/config.json", False),
        (Kind.CREDENTIALS, ".copilot/providers.json", False),
    }


def test_copilot_project_files(more_project):
    assert _rel(Copilot().discover_project(more_project), more_project) == {
        (Kind.SETTINGS, ".github/copilot/settings.json", False),
        (Kind.SETTINGS, ".github/copilot/settings.local.json", True),
        (Kind.SETTINGS, ".github/hooks/format.json", False),
        (Kind.SETTINGS, ".vscode/mcp.json", False),
    }


def test_copilot_grants(more_home):
    cli = _config(more_home / ".copilot" / "settings.json")
    assert [(g.setting, g.value) for g in cli.grants] == [("defaultPermissionMode", "allow-all")]
    vscode = _config(more_home / CODE / "settings.json")
    assert [(g.setting, g.value) for g in vscode.grants] == [
        ("chat.tools.global.autoApprove", "true")
    ]


def test_vscode_terminal_auto_approve_rules(tmp_path):
    f = tmp_path / "settings.json"
    rules = {"git status": True, "/.*/": True, "bash": True, "rm": False}
    f.write_text(json.dumps({"chat.tools.terminal.autoApprove": rules}))
    assert [g.value for g in _config(f).grants] == ["/.*/", "bash"]


def test_copilot_hooks_use_bash_powershell_and_exec_fields(more_home, tmp_path):
    assert _config(more_home / ".copilot" / "hooks" / "notify.json").hooks == (
        HookCommand("sessionEnd", "curl -s https://hooks.example.invalid/x"),
    )
    f = tmp_path / "hooks" / "h.json"
    f.parent.mkdir()
    handler = {"type": "command", "powershell": "iwr x", "exec": "wget", "args": ["-q", "u"]}
    f.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": [handler]}}))
    assert _config(f).hooks == (
        HookCommand("preToolUse", "iwr x"),
        HookCommand("preToolUse", "wget -q u"),
    )


def test_copilot_inline_settings_hooks(tmp_path):
    f = tmp_path / ".github" / "copilot" / "settings.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"hooks": {"agentStop": [{"type": "command", "bash": "nc h 1"}]}}))
    assert _config(f).hooks == (HookCommand("agentStop", "nc h 1"),)


def test_copilot_mcp_files(more_home, more_project, tmp_path):
    [cli] = _config(more_home / ".copilot" / "mcp-config.json").mcp_servers
    assert cli.name == "context7" and "chmod 600" in cli.secret_fix
    [vscode] = _config(more_project / ".vscode" / "mcp.json").mcp_servers
    assert vscode.name == "billing" and "${input:<VAR>}" in vscode.secret_fix
    bare = tmp_path / ".github" / "mcp.json"
    bare.parent.mkdir(parents=True)
    bare.write_text(json.dumps({"gh": {"command": "npx"}}))  # project files may skip the wrapper
    assert [s.name for s in _config(bare).mcp_servers] == ["gh"]


def test_credential_stores_are_never_secret_scanned(more_home):
    findings = scan(more_home, None)
    names = {Path(f.location).name for f in findings}
    assert "config.json" not in names and "providers.json" not in names
    history = {Path(f.location).name for f in findings if f.check_id == "AP001"}
    assert history == {"events.jsonl", "s1.json"}


def test_copilot_package_rules():
    def matches(name):
        return [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(name)]

    assert matches("package/.github/copilot/settings.local.json") == ["copilot"]
    assert matches("package/.copilot/session-state/s/events.jsonl") == ["copilot"]
    assert matches("package/.copilot/providers.json") == ["copilot"]
    assert matches("package/.copilot/session-store.db") == ["copilot"]
    assert matches("x/workspaceStorage/ab/chatSessions/s.jsonl") == ["copilot"]
    assert matches("package/.vscode/mcp.json") == ["copilot"]
    assert matches("package/.github/copilot/settings.json") == []
    assert matches("package/.github/copilot-instructions.md") == []
    assert matches("package/.github/hooks/format.json") == []
    assert matches("package/.github/workflows/ci.yml") == []
    assert matches("package/.vscode/settings.json") == []
    assert matches("package/src/copilot.ts") == []
