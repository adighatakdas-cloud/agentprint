from agentprint.agents import ALL_AGENTS
from agentprint.agents.claude_code import ClaudeCode
from agentprint.agents.codex import Codex
from agentprint.models import Kind


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix()) for a in artifacts}


def test_claude_code_home_files(fake_home):
    found = _rel(ClaudeCode().discover_home(fake_home), fake_home)
    assert found == {
        (Kind.HISTORY, ".claude/history.jsonl"),
        (Kind.SETTINGS, ".claude/settings.json"),
        (Kind.HISTORY, ".claude/paste-cache/abc.txt"),
        (Kind.HISTORY, ".claude/projects/-tmp-demo/s1.jsonl"),
    }


def test_codex_home_files_list_auth_json_only_as_a_credential_store(fake_home):
    found = _rel(Codex().discover_home(fake_home), fake_home)
    assert found == {
        (Kind.SETTINGS, ".codex/config.toml"),
        (Kind.HISTORY, ".codex/sessions/2026/10/01/rollout-x.jsonl"),
        (Kind.HISTORY, ".codex/history.jsonl"),
        (Kind.CREDENTIALS, ".codex/auth.json"),  # never secret-scanned; permission check only
    }


def test_claude_code_linux_credentials_file_is_a_credential_store(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    found = _rel(ClaudeCode().discover_home(tmp_path), tmp_path)
    assert found == {(Kind.CREDENTIALS, ".claude/.credentials.json")}


def test_claude_code_project_files(fake_project):
    found = _rel(ClaudeCode().discover_project(fake_project), fake_project)
    assert found == {(Kind.SETTINGS, ".claude/settings.local.json")}


def test_empty_home_has_no_artifacts(tmp_path):
    for agent in ALL_AGENTS:
        assert list(agent.discover_home(tmp_path)) == []
        assert list(agent.discover_project(tmp_path)) == []


def test_package_rules_match_agent_files_only():
    def matches(name):
        return [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(name)]

    assert matches("package/.claude/settings.local.json") == ["claude-code"]
    assert matches(".claude/projects/x/abc.jsonl") == ["claude-code"]
    assert matches("pkg-1.0/CLAUDE.local.md") == ["claude-code"]
    assert matches("package/.codex/sessions/2026/01/01/r.jsonl") == ["codex"]
    assert matches("package/.codex/config.toml") == ["codex"]
    assert matches("package/.claude/settings.json") == []
    assert matches("package/CLAUDE.md") == []
    assert matches("package/src/index.js") == []
