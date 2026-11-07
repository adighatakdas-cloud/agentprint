"""Where each agent loads extensions from (agent modules' extension_paths), and how a scan lists
them: Kind.EXTENSION, the first agent to name a file keeps it, apart from the key checks."""

from pathlib import Path

import pytest

from agentprint.agents import ALL_AGENTS
from agentprint.agents.base import NOT_LOADED, ExtensionArtifact, children, each
from agentprint.models import Kind
from agentprint.scanner import discover
from tests.fixtures import extensions as ext

# (agent, "home" or "project", path relative to it): one file each agent loads.
LOADED = [
    ("claude-code", "home", ".claude/CLAUDE.md"),
    ("claude-code", "home", ".claude/skills/review/SKILL.md"),
    ("claude-code", "home", ".claude/skills/review/scripts/run.sh"),
    ("claude-code", "home", ".claude/commands/deploy.md"),
    ("claude-code", "home", ".claude/agents/reviewer.md"),
    ("claude-code", "home", ".claude/rules/style.md"),
    ("claude-code", "home", ".claude/hooks/check.sh"),
    ("claude-code", "home", ".claude/plugins/cache/market/tool/1.0.0/skills/x/SKILL.md"),
    ("claude-code", "project", "CLAUDE.md"),
    ("claude-code", "project", ".claude/CLAUDE.md"),
    ("claude-code", "project", ".claude/commands/test.md"),
    ("claude-code", "project", ".claude/skills/x/SKILL.md"),
    ("codex", "home", ".codex/AGENTS.md"),
    ("codex", "home", ".codex/prompts/fix.md"),
    ("codex", "home", ".codex/skills/x/SKILL.md"),
    ("codex", "home", ".agents/skills/x/SKILL.md"),
    ("codex", "home", ".codex/plugins/cache/market/tool/1.0.0/hooks/hooks.json"),
    ("codex", "project", "AGENTS.md"),
    ("codex", "project", ".agents/skills/x/SKILL.md"),
    ("cursor", "home", ".cursor/rules/agentprint.mdc"),
    ("cursor", "home", ".cursor/plugins/local/tool/.cursor-plugin/plugin.json"),
    ("cursor", "project", ".cursorrules"),
    ("cursor", "project", ".cursor/rules/style.mdc"),
    ("cursor", "project", ".cursor/commands/review.md"),
    ("gemini-cli", "home", ".gemini/GEMINI.md"),
    ("gemini-cli", "home", ".gemini/commands/review.toml"),
    ("gemini-cli", "home", ".gemini/extensions/tool/gemini-extension.json"),
    ("gemini-cli", "project", "GEMINI.md"),
    ("gemini-cli", "project", ".gemini/commands/review.toml"),
    ("windsurf", "home", ".codeium/windsurf/memories/global_rules.md"),
    ("windsurf", "home", ".codeium/windsurf/skills/x/SKILL.md"),
    ("windsurf", "home", ".devin/rules/agentprint.md"),
    ("windsurf", "project", ".windsurfrules"),
    ("windsurf", "project", ".windsurf/rules/style.md"),
    ("windsurf", "project", ".windsurf/workflows/release.md"),
    ("opencode", "home", ".config/opencode/AGENTS.md"),
    ("opencode", "home", ".config/opencode/plugins/notify.js"),
    ("opencode", "project", ".opencode/agent/review.md"),
    ("copilot", "home", ".copilot/copilot-instructions.md"),
    ("copilot", "home", ".copilot/skills/x/SKILL.md"),
    ("copilot", "home", ".copilot/installed-plugins/market/tool/plugin.json"),
    ("copilot", "home", "Library/Application Support/Code/User/prompts/a.prompt.md"),
    ("copilot", "project", ".github/copilot-instructions.md"),
    ("copilot", "project", ".github/instructions/py.instructions.md"),
    ("copilot", "project", ".github/prompts/review.prompt.md"),
    ("hermes", "home", ".hermes/SOUL.md"),
    ("hermes", "home", ".hermes/skills/demo/SKILL.md"),
    ("hermes", "home", ".hermes/plugins/tool/__init__.py"),
    ("hermes", "project", "HERMES.md"),
    ("antigravity", "home", ".gemini/config/rules/agentprint.md"),
    ("antigravity", "home", ".gemini/config/plugins/tool/plugin.json"),
    ("antigravity", "project", ".agents/rules/style.md"),
    ("antigravity", "project", ".agents/workflows/release.md"),
    ("cline", "home", "Documents/Cline/Rules/style.md"),
    ("cline", "home", ".agents/AGENTS.md"),
    ("cline", "project", ".clinerules/style.md"),
    ("cline", "project", ".clinerules/hooks/PreToolUse"),
    ("kilo", "project", ".kilo/rules/style.md"),
    ("kilo", "project", ".kilocode/rules/style.md"),
    ("kiro", "home", ".kiro/steering/style.md"),
    ("kiro", "project", ".kiro/steering/product.md"),
    ("continue", "project", ".continue/rules/style.md"),
    ("goose", "home", ".config/goose/.goosehints"),
    ("goose", "home", ".agents/plugins/tool/hooks/check.sh"),
    ("goose", "project", ".goosehints"),
    ("qwen-code", "home", ".qwen/QWEN.md"),
    ("qwen-code", "home", ".qwen/commands/review.toml"),
    ("qwen-code", "project", ".qwen/agents/reviewer.md"),
    ("factory-droid", "home", ".factory/droids/reviewer.md"),
    ("factory-droid", "project", ".factory/commands/deploy.md"),
    ("augment", "home", ".augment/rules/style.md"),
    ("augment", "project", ".augment-guidelines"),
    ("augment", "project", ".augment/commands/deploy.md"),
    ("junie", "project", ".junie/guidelines.md"),
    ("junie", "project", ".junie/AGENTS.md"),
    ("amp", "home", ".config/amp/plugins/notify.ts"),
    ("amp", "project", ".amp/plugins/notify.ts"),
    ("crush", "home", ".config/crush/CRUSH.md"),
    ("crush", "project", ".crush/skills/x/SKILL.md"),
    ("zed", "project", ".rules"),
    ("zed", "home", ".config/zed/AGENTS.md"),
]


@pytest.fixture
def world(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    for _, where, rel in LOADED:
        ext.write((home if where == "home" else project) / rel, "Use tabs.\n")
    return home, project


def test_each_agent_names_the_files_it_loads(world):
    home, project = world
    found = {
        (a.agent, a.path.relative_to(home if a.path.is_relative_to(home) else project).as_posix())
        for a in discover(home, project)
        if a.kind is Kind.EXTENSION
    }
    assert set((agent, rel) for agent, _, rel in LOADED) <= found
    assert all(
        isinstance(a, ExtensionArtifact) and a.root is not None
        for a in discover(home, project)
        if a.kind is Kind.EXTENSION
    )


def test_every_agent_answers_extension_paths(tmp_path):
    for agent in ALL_AGENTS:
        assert isinstance(agent.extension_paths(tmp_path, tmp_path), list)
        assert agent.extension_paths(None, None) == []


def test_extensions_are_listed_apart_from_the_key_checks(world, more_home):
    # Hermes skills are history (AP001 reads keys there) and an extension (AP010).
    kinds = {
        a.kind
        for a in discover(more_home, None)
        if a.path.as_posix().endswith(".hermes/skills/demo/SKILL.md")
    }
    assert kinds == {Kind.HISTORY, Kind.EXTENSION}


def test_shared_files_belong_to_the_first_agent(world):
    home, project = world
    owners = [a.agent for a in discover(home, project) if a.path == project / "AGENTS.md"]
    assert owners == ["codex"]  # Copilot, Cursor, Zed, Amp and others read it too


def test_dependency_folders_are_not_read(tmp_path):
    plugin = tmp_path / "home" / ".claude" / "plugins" / "cache" / "m" / "p" / "1.0.0"
    for folder in sorted(NOT_LOADED):
        ext.write(plugin / folder / "x.md", "x\n")
    ext.write(plugin / "skills" / "x" / "SKILL.md", "x\n")
    found = [a.path for a in discover(tmp_path / "home", None) if a.kind is Kind.EXTENSION]
    assert found == [plugin / "skills" / "x" / "SKILL.md"]


def test_one_root_per_entry(tmp_path):
    ext.write(tmp_path / "rules" / "a.md", "x")
    ext.write(tmp_path / "rules" / "b" / "c.md", "x")
    assert children(tmp_path / "rules", tmp_path / "missing") == [
        tmp_path / "rules" / "a.md",
        tmp_path / "rules" / "b",
    ]
    ext.write(tmp_path / ".clinerules", "x")
    assert each(tmp_path / ".clinerules", tmp_path / "rules") == [
        tmp_path / ".clinerules",
        tmp_path / "rules" / "a.md",
        tmp_path / "rules" / "b",
    ]


@pytest.mark.posix_only
def test_a_symlinked_skill_is_followed_and_links_inside_it_are_not(tmp_path):
    real = tmp_path / "dotfiles" / "review"
    ext.write(real / "SKILL.md", "x\n")
    ext.write(tmp_path / "secret.md", "x\n")
    (real / "linked.md").symlink_to(tmp_path / "secret.md")
    skills = tmp_path / "home" / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "review").symlink_to(real)
    found = [a.path.name for a in discover(tmp_path / "home", None) if a.kind is Kind.EXTENSION]
    assert found == ["SKILL.md"]


def test_extension_files_are_not_permission_checked(tmp_path):
    from agentprint.checks import ap008_readable_by_others as ap008

    ext.write(tmp_path / "home" / ".claude" / "skills" / "x" / "SKILL.md", "x\n")
    artifacts = [a for a in discover(tmp_path / "home", None) if a.kind is Kind.EXTENSION]
    assert artifacts and not any(ap008._in_scope(a) for a in artifacts)


def test_paths_need_no_home(tmp_path):
    project = tmp_path / "proj"
    ext.write(project / ".claude" / "commands" / "x.md", "x\n")
    assert [a.path for a in discover(None, project) if a.kind is Kind.EXTENSION] == [
        Path(project / ".claude" / "commands" / "x.md")
    ]
