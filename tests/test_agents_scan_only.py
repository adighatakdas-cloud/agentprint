"""Scan-only agents: package rules (AP006) for agents agentprint doesn't fully read yet."""

import pytest

from agentprint.agents import ALL_AGENTS
from agentprint.agents.scan_only import SCAN_ONLY_AGENTS
from agentprint.models import AgentConfig, Artifact, Kind
from agentprint.scanner import scan
from tests import fakes


def _agents_matching(name):
    return [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(name)]


@pytest.mark.parametrize(
    ("member", "agent"),
    [
        ("package/.aider.chat.history.md", "aider"),
        ("package/.aider.input.history", "aider"),
        ("package/.aider.llm.history", "aider"),
        ("package/.aider.conf.yml", "aider"),
        ("package/.aider.tags.cache.v4/cache.db", "aider"),
        ("package/.cline/data/secrets.json", "cline"),
        ("package/.local/share/kilo/kilo.db", "kilo"),
        ("package/.kilo/kilo.jsonc", "kilo"),
        ("package/.kilocode/mcp.json", "kilo"),
        ("package/.qwen/.env", "qwen-code"),
        ("package/.qwen/settings.json", "qwen-code"),
        ("package/.qwen/QWEN.local.md", "qwen-code"),
        ("package/.qwen/projects/p/chats/s.jsonl", "qwen-code"),
        ("package/.crush/crush.db", "crush"),
        ("package/.config/goose/secrets.yaml", "goose"),
        ("package/.local/share/goose/sessions/sessions.db", "goose"),
        ("package/.kiro/settings/mcp.json", "kiro"),
        ("package/.factory/settings.local.json", "factory-droid"),
        ("package/.factory/sessions/x/s.jsonl", "factory-droid"),
        ("package/.continue/.env", "continue"),
        ("package/.continue/logs/prompt.log", "continue"),
        ("package/Zed/threads/threads.db", "zed"),
        ("package/.pi/agent/auth.json", "pi"),
        ("package/.pi/mcp.json", "pi"),
        ("package/.openclaw/.env", "openclaw"),
        ("package/.clawdbot/credentials/oauth.json", "openclaw"),
        ("package/warp.sqlite", "warp"),
        ("package/.roo/mcp.json", "roo-code"),
        ("package/roo-code-settings.json", "roo-code"),
        ("package/.augment/session.json", "augment"),
    ],
)
def test_each_rule_matches_its_file_for_one_agent(member, agent):
    assert _agents_matching(member) == [agent]


@pytest.mark.parametrize(
    "member",
    [
        # Instructions, rules and shared-by-design config are not leaks.
        "package/AGENTS.md",
        "package/CLAUDE.md",
        "package/.clinerules/style.md",
        "package/.cline/rules/style.md",
        "package/.kilocode/rules/style.md",
        "package/.goosehints",
        "package/.agents/plugins/lint/hooks/hooks.json",
        "package/.kiro/steering/product.md",
        "package/.kiro/specs/feature/design.md",
        "package/.factory/settings.json",
        "package/.continue/rules/style.md",
        "package/.zed/settings.json",
        "package/.pi/settings.json",
        "package/.roo/rules/style.md",
        "package/QWEN.md",
        "package/CRUSH.md",
        "package/.crushignore",
        "package/.aiderignore",
        "package/docs/aider.md",
        "package/.env.example",
        "package/src/warp.ts",
    ],
)
def test_instruction_files_and_look_alikes_are_not_flagged(member):
    assert _agents_matching(member) == []


def test_scan_only_agents_have_no_home_files_and_no_config(tmp_path):
    for agent in SCAN_ONLY_AGENTS:
        assert list(agent.discover_home(tmp_path)) == []
        assert agent.read_config(Artifact(agent.name, Kind.SETTINGS, tmp_path)) == AgentConfig()
        assert all(rule.agent == agent.name for rule in agent.package_rules)


def test_aider_transcripts_in_the_repo_are_scanned_and_personal(tmp_path):
    (tmp_path / ".aider.chat.history.md").write_text(f"#### deploy with {fakes.GITHUB}\n")
    (tmp_path / ".aider.input.history").write_text("+ hello\n")
    (tmp_path / ".qwen").mkdir()
    (tmp_path / ".qwen" / ".env").write_text(f"OPENAI_API_KEY={fakes.OPENAI}\n")
    found = [a for agent in SCAN_ONLY_AGENTS for a in agent.discover_project(tmp_path)]
    assert {(a.path.name, a.kind, a.personal) for a in found} == {
        (".aider.chat.history.md", Kind.HISTORY, True),
        (".aider.input.history", Kind.HISTORY, True),
        (".env", Kind.CREDENTIALS, True),
    }
    [finding] = scan(None, tmp_path)
    assert (finding.check_id, finding.agent) == ("AP001", "aider")
