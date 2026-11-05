from pathlib import Path

from agentprint.agents import ALL_AGENTS
from agentprint.agents.antigravity import Antigravity
from agentprint.agents.gemini import Gemini
from agentprint.models import Artifact, HookCommand, Kind
from agentprint.scanner import scan


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix()) for a in artifacts}


def _config(path):
    return Antigravity().read_config(Artifact("antigravity", Kind.SETTINGS, path))


def test_antigravity_home_files(more_home):
    logs = "brain/c-0001/.system_generated/logs"
    assert _rel(Antigravity().discover_home(more_home), more_home) == {
        (Kind.SETTINGS, ".gemini/config/hooks.json"),
        (Kind.SETTINGS, ".gemini/config/mcp_config.json"),
        (Kind.SETTINGS, ".gemini/antigravity/mcp_config.json"),
        (Kind.SETTINGS, ".gemini/antigravity-cli/settings.json"),
        (Kind.HISTORY, f".gemini/antigravity/{logs}/transcript.jsonl"),
        (Kind.HISTORY, ".gemini/antigravity/brain/c-0001/task.md"),
        (Kind.CREDENTIALS, ".gemini/antigravity/mcp_oauth_tokens.json"),
    }


def test_gemini_cli_does_not_claim_antigravity_files(more_home):
    assert list(Gemini().discover_home(more_home)) == []


def test_antigravity_project_files(more_project):
    assert _rel(Antigravity().discover_project(more_project), more_project) == {
        (Kind.SETTINGS, ".agents/hooks.json"),
        (Kind.SETTINGS, ".agents/mcp_config.json"),
    }


def test_antigravity_hooks_are_grouped_by_hook_set_and_skip_disabled(more_home, more_project):
    assert _config(more_home / ".gemini" / "config" / "hooks.json").hooks == (
        HookCommand("PreToolUse", "./guard.sh"),
        HookCommand("Stop", "wget -q https://hooks.example.invalid/done"),
    )
    assert _config(more_project / ".agents" / "hooks.json").hooks == (
        HookCommand("PostToolUse", "./scripts/lint.sh"),
    )


def test_antigravity_cli_permissions(more_home):
    config = _config(more_home / ".gemini" / "antigravity-cli" / "settings.json")
    assert [(g.setting, g.value) for g in config.grants] == [
        ("toolPermission", "always-proceed"),
        ("permissions.allow", "command(*)"),
    ]


def test_antigravity_mcp_servers(more_home, more_project):
    [maps] = _config(more_home / ".gemini" / "config" / "mcp_config.json").mcp_servers
    assert maps.name == "maps" and "chmod 600" in maps.secret_fix
    [aws] = _config(more_project / ".agents" / "mcp_config.json").mcp_servers
    assert aws.name == "aws"
    assert _config(more_home / ".gemini" / "antigravity" / "mcp_config.json").mcp_servers == ()


def test_antigravity_scan(more_home, more_project):
    findings = [f for f in scan(more_home, more_project) if f.agent == "antigravity"]
    assert sorted({f.check_id for f in findings}) == ["AP001", "AP003", "AP004", "AP005"]
    assert "mcp_oauth_tokens.json" not in {Path(f.location).name for f in findings}


def test_antigravity_package_rules():
    def matches(name):
        return [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(name)]

    assert matches("package/.gemini/antigravity/brain/c/task.md") == ["antigravity"]
    assert matches("package/.gemini/antigravity-cli/settings.json") == ["antigravity"]
    assert matches("package/.gemini/config/mcp_config.json") == ["antigravity"]
    assert matches("package/.agents/mcp_config.json") == ["antigravity"]
    assert matches("package/.agents/hooks.json") == []
    assert matches("package/.agents/rules/style.md") == []
    assert matches("package/.agents/skills/x/SKILL.md") == []
    assert matches("package/AGENTS.md") == []
    assert matches("package/GEMINI.md") == []
