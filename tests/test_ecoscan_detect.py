import json

import pytest

from agentprint.secrets import redact
from tests import fakes
from tools.ecoscan.detect import candidate, is_candidate, secret_findings


@pytest.mark.parametrize(
    ("path", "agent", "kind", "ap006"),
    [
        (".claude/settings.local.json", "claude-code", "settings", True),
        ("sub/.claude/settings.json", "claude-code", "settings", False),
        (".claude/projects/x/s1.jsonl", "claude-code", "history", True),
        (".claude/commands/review.md", "claude-code", "instructions", False),
        (".claude.json", "claude-code", "settings", True),
        ("CLAUDE.local.md", "claude-code", "instructions", True),
        ("CLAUDE.md", "claude-code", "instructions", False),
        (".mcp.json", "mcp", "settings", None),
        (".codex/config.toml", "codex", "settings", True),
        (".codex/sessions/2026/10/01/rollout-x.jsonl", "codex", "history", True),
        ("AGENTS.md", "agents-md", "instructions", None),
        (".cursor/mcp.json", "cursor", "settings", None),
        (".cursor/rules/style.mdc", "cursor", "instructions", None),
        (".cursorrules", "cursor", "instructions", None),
        (".gemini/settings.json", "gemini", "settings", None),
        ("GEMINI.md", "gemini", "instructions", None),
        (".windsurf/rules/a.md", "windsurf", "instructions", None),
        (".codeium/windsurf/mcp_config.json", "windsurf", "settings", None),
        (".opencode/agent/a.md", "opencode", "instructions", None),
        ("opencode.json", "opencode", "settings", None),
        (".aider.chat.history.md", "aider", "history", None),
        (".aider.input.history", "aider", "history", None),
        (".aider.conf.yml", "aider", "settings", None),
        (".continue/config.yaml", "continue", "settings", None),
        (".clinerules", "cline", "instructions", None),
        (".roo/rules/a.md", "roo", "instructions", None),
        (".kiro/steering/a.md", "kiro", "instructions", None),
        (".github/copilot-instructions.md", "copilot", "instructions", None),
        (".junie/guidelines.md", "junie", "instructions", None),
    ],
)
def test_candidate_paths_are_classified(path, agent, kind, ap006):
    # ap006=None: agentprint has no package rule for this agent yet (added on other branches).
    got = candidate("package/" + path)
    assert got is not None
    assert (got.agent, got.kind) == (agent, kind)
    if ap006 is not None:
        assert got.ap006 == ap006
    assert got.path == "package/" + path


@pytest.mark.parametrize(
    "path",
    [
        "src/claude.js",
        "docs/cursor.md",
        ".github/workflows/ci.yml",
        "lib/codex/index.js",
        "README.md",
        "my.claude.json.bak",
        "notclaude/x",
    ],
)
def test_ordinary_paths_are_not_candidates(path):
    assert candidate(path) is None
    assert not is_candidate(path)


def test_secret_findings_are_redacted_with_line_and_count():
    data = f"one\nkey {fakes.ANTHROPIC}\nagain {fakes.ANTHROPIC}\n{fakes.GITHUB}\n".encode()
    found = secret_findings(data)
    assert found == [
        {"line": 2, "rule": "anthropic", "redacted": redact(fakes.ANTHROPIC), "count": 2},
        {"line": 4, "rule": "github", "redacted": redact(fakes.GITHUB), "count": 1},
    ]
    blob = json.dumps(found)
    assert fakes.ANTHROPIC not in blob
    assert fakes.GITHUB not in blob


def test_binary_content_is_not_scanned():
    assert secret_findings(b"\x00\x01PNG" + fakes.AWS.encode()) == []
