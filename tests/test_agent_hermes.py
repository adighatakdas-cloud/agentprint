from pathlib import Path

from agentprint.agents import ALL_AGENTS
from agentprint.agents.hermes import Hermes
from agentprint.models import Artifact, HookCommand, Kind
from agentprint.scanner import scan


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix()) for a in artifacts}


def _config(path):
    return Hermes().read_config(Artifact("hermes", Kind.SETTINGS, path))


def test_hermes_home_files(more_home):
    assert _rel(Hermes().discover_home(more_home), more_home) == {
        (Kind.SETTINGS, ".hermes/config.yaml"),
        (Kind.SETTINGS, ".hermes/profiles/work/config.yaml"),
        (Kind.HISTORY, ".hermes/state.db"),
        (Kind.HISTORY, ".hermes/memories/MEMORY.md"),
        (Kind.HISTORY, ".hermes/SOUL.md"),
        (Kind.HISTORY, ".hermes/skills/demo/SKILL.md"),
        (Kind.CREDENTIALS, ".hermes/.env"),
        (Kind.CREDENTIALS, ".hermes/auth.json"),
        (Kind.CREDENTIALS, ".hermes/profiles/work/.env"),
    }


def test_hermes_has_no_project_files(more_project):
    assert list(Hermes().discover_project(more_project)) == []


def test_hermes_config_grants_hooks_and_servers(more_home):
    config = _config(more_home / ".hermes" / "config.yaml")
    # YAML reads an unquoted `mode: off` as the boolean false; it still means "no approvals".
    assert [(g.setting, g.value) for g in config.grants] == [
        ("approvals.mode", "off"),
        ("command_allowlist", "*"),
        ("hooks_auto_accept", "true"),
    ]
    assert config.hooks == (
        HookCommand("pre_tool_call", "~/.hermes/agent-hooks/guard.sh"),
        HookCommand("post_tool_call", "curl -s https://hooks.example.invalid/x"),
    )
    assert [s.name for s in config.mcp_servers] == ["github", "docs"]
    assert "${env:<VAR>}" in config.mcp_servers[0].secret_fix


def test_hermes_safe_settings_have_no_grants(more_home):
    assert _config(more_home / ".hermes" / "profiles" / "work" / "config.yaml").grants == ()


def test_hermes_history_db_is_scanned_and_key_stores_are_not(more_home):
    findings = [f for f in scan(more_home, None) if f.agent == "hermes"]
    assert {(f.check_id, Path(f.location).name) for f in findings if f.check_id == "AP001"} == {
        ("AP001", "state.db")
    }
    assert not {".env", "auth.json"} & {Path(f.location).name for f in findings}
    assert {f.check_id for f in findings} == {"AP001", "AP003", "AP004", "AP005"}


def test_hermes_package_rules():
    def matches(name):
        return [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(name)]

    assert matches("package/.hermes/.env") == ["hermes"]
    assert matches("package/.hermes/state.db") == ["hermes"]
    assert matches("package/.hermes/memories/MEMORY.md") == ["hermes"]
    assert matches("package/HERMES.md") == []
    assert matches("package/.hermes.md") == []
    assert matches("package/docs/hermes/setup.md") == []
