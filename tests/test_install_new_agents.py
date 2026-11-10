"""install / uninstall for GitHub Copilot, Hermes Agent and Google Antigravity. Fake homes only."""

import json
from pathlib import Path

import pytest
import yaml

from agentprint import install

QUIET = lambda line: None  # noqa: E731
AGENTPRINT = "/opt/agentprint/bin/agentprint"

HERMES_CONFIG = """\
# Hermes settings: my own notes stay here.
model:
  default: nous/hermes-4   # pinned on purpose

mcp_servers:
  github:
    command: gh-mcp          # keeps its comment
    args: [serve]

hooks:
  post_tool_call:
    - matcher: "terminal"
      command: "~/bin/log-commands.sh"
"""


@pytest.fixture(autouse=True)
def fixed_launch(monkeypatch):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: [AGENTPRINT])


def _install(home, names=None, dry_run=False):
    out: list[str] = []
    install.install(home, {}, names, dry_run, out.append)
    return "\n".join(out)


def _uninstall(home, names=None):
    out: list[str] = []
    install.uninstall(home, {}, names, False, out.append)
    return "\n".join(out)


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(root).as_posix(): (None if p.is_dir() else p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    _write(home / ".copilot" / "mcp-config.json", '{"mcpServers": {"other": {"command": "x"}}}\n')
    _write(home / ".hermes" / "config.yaml", HERMES_CONFIG)
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    return home


# ---- GitHub Copilot ---------------------------------------------------------------------------


@pytest.mark.posix_only
def test_copilot_gets_a_hook_file_mcp_entry_and_instructions(home):
    _install(home, ["copilot"])
    hooks = json.loads((home / ".copilot" / "hooks" / "agentprint.json").read_text())
    [entry] = hooks["hooks"]["preToolUse"]
    assert hooks["version"] == 1 and entry["matcher"] == "bash"
    assert entry["bash"].endswith("agentprint-hook --agent copilot")
    mcp = json.loads((home / ".copilot" / "mcp-config.json").read_text())
    assert mcp["mcpServers"]["agentprint"] == {
        "type": "local",
        "command": AGENTPRINT,
        "args": ["mcp"],
        "tools": ["*"],
    }
    assert mcp["mcpServers"]["other"] == {"command": "x"}
    assert "agentprint:start" in (home / ".copilot" / "copilot-instructions.md").read_text()


# ---- Hermes Agent -----------------------------------------------------------------------------


@pytest.mark.posix_only
def test_hermes_config_gets_hook_and_server_and_keeps_comments(home):
    out = _install(home, ["hermes"])
    text = (home / ".hermes" / "config.yaml").read_text()
    for kept in ("# Hermes settings: my own notes stay here.", "# pinned on purpose",
                 "# keeps its comment"):  # fmt: skip
        assert kept in text
    data = yaml.safe_load(text)
    [ours] = [h for h in data["hooks"]["pre_tool_call"] if "agentprint-hook" in h["command"]]
    assert ours["matcher"] == "terminal" and ours["command"].endswith("--agent hermes")
    assert data["hooks"]["post_tool_call"][0]["command"] == "~/bin/log-commands.sh"
    assert data["mcp_servers"]["agentprint"] == {
        "command": AGENTPRINT,
        "args": ["mcp"],
        "enabled": True,
    }
    assert data["mcp_servers"]["github"] == {"command": "gh-mcp", "args": ["serve"]}
    assert data["model"] == {"default": "nous/hermes-4"}
    # Never approves its own hook, and leaves the persona file alone.
    assert not (home / ".hermes" / "shell-hooks-allowlist.json").exists()
    assert not (home / ".hermes" / "SOUL.md").exists()
    assert "inactive until you approve it" in out and "hermes chat" in out


@pytest.mark.posix_only  # asserts hook entries; Windows installs no hooks
def test_hermes_users_lines_stay_byte_for_byte(home):
    _install(home, ["hermes"])
    lines = (home / ".hermes" / "config.yaml").read_text().splitlines()
    assert [ln for ln in lines if ln in HERMES_CONFIG.splitlines()] == HERMES_CONFIG.splitlines()
    assert "    - matcher: terminal" in lines  # ours follow the file's own list style


@pytest.mark.posix_only  # asserts hook entries; Windows installs no hooks
def test_hermes_flush_list_style_is_followed(tmp_path):
    home = tmp_path / "home"
    flush = "hooks:\n  post_tool_call:\n  - matcher: terminal\n    command: x\n"
    _write(home / ".hermes" / "config.yaml", flush)
    _install(home, ["hermes"])
    text = (home / ".hermes" / "config.yaml").read_text()
    remaining = iter(text.splitlines())
    assert all(line in remaining for line in flush.splitlines())  # in order, unchanged
    assert "\n  - matcher: terminal\n    command: /" in text  # ours in the same flush style


@pytest.mark.posix_only
def test_hermes_without_config_yaml_is_left_alone_and_shown_the_snippet(tmp_path):
    # A config.yaml holding only agentprint's keys might stop Hermes applying its defaults, so
    # agentprint never creates one: it prints what to add and how to get Hermes to make it.
    home = tmp_path / "home"
    (home / ".hermes" / "skills").mkdir(parents=True)
    before = snapshot(home)
    out = _install(home, ["hermes"])
    assert snapshot(home) == before  # nothing written anywhere, nothing recorded
    assert "run Hermes once" in out and "agentprint install again" in out
    snippet = yaml.safe_load(
        "\n".join(line[6:] for line in out.splitlines() if line.startswith("      "))
    )
    assert snippet["mcp_servers"]["agentprint"]["command"] == AGENTPRINT
    assert snippet["hooks"]["pre_tool_call"][0]["matcher"] == "terminal"


def test_hermes_unreadable_yaml_is_skipped_with_a_snippet(tmp_path):
    home = tmp_path / "home"
    _write(home / ".hermes" / "config.yaml", "model: [unclosed\n")
    out = _install(home, ["hermes"])
    assert (home / ".hermes" / "config.yaml").read_text() == "model: [unclosed\n"
    assert "by hand" in out


# ---- Google Antigravity -----------------------------------------------------------------------


@pytest.mark.posix_only
def test_antigravity_gets_hooks_mcp_and_a_rule_under_gemini_config(home):
    _install(home, ["antigravity"])
    config = home / ".gemini" / "config"
    hooks = json.loads((config / "hooks.json").read_text())
    [group] = hooks["agentprint"]["PreToolUse"]
    assert group["matcher"] == "run_command"
    assert group["hooks"][0]["command"].endswith("agentprint-hook --agent antigravity")
    mcp = json.loads((config / "mcp_config.json").read_text())
    assert mcp["mcpServers"]["agentprint"] == {"command": AGENTPRINT, "args": ["mcp"]}
    rule = (config / "rules" / "agentprint.md").read_text()
    assert rule.startswith("---\ntrigger: always_on\n---\n")
    assert not (home / ".gemini" / "settings.json").exists()  # Gemini CLI's file: not ours
    assert not (home / ".gemini" / "GEMINI.md").exists()


def test_gemini_cli_and_antigravity_never_touch_each_others_files(tmp_path):
    home = tmp_path / "home"
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    _write(home / ".gemini" / "settings.json", '{"theme": "dark"}\n')
    before = snapshot(home)
    _install(home, ["gemini", "antigravity"])
    gemini_files = {k: v for k, v in snapshot(home).items() if not k.startswith(".gemini/config")}
    _uninstall(home, ["antigravity"])
    after = snapshot(home)
    assert not any(k.startswith(".gemini/config") for k in after)  # antigravity's are gone
    for name in (".gemini/settings.json", ".gemini/GEMINI.md"):  # Gemini CLI's are intact
        assert after[name] == gemini_files[name]
    _uninstall(home, ["gemini"])
    assert snapshot(home) == before


# ---- all three --------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["copilot", "hermes", "antigravity"])
def test_round_trip_is_byte_identical_and_reinstall_changes_nothing(home, agent):
    before = snapshot(home)
    _install(home, [agent])
    first = snapshot(home)
    assert first != before
    assert "Nothing changed" in _install(home, [agent])
    assert snapshot(home) == first
    _uninstall(home)
    assert snapshot(home) == before


@pytest.mark.posix_only  # asserts hook entries; Windows installs no hooks
def test_dry_run_over_all_nine_agents_is_a_coherent_plan(tmp_path):
    home = tmp_path / "home"
    for folder in (".claude", ".codex", ".cursor", ".codeium/windsurf", ".devin",
                   ".config/opencode", ".copilot", ".hermes", ".gemini/antigravity"):  # fmt: skip
        (home / folder).mkdir(parents=True)
    _write(home / ".gemini" / "settings.json", "{}\n")
    _write(home / ".hermes" / "config.yaml", HERMES_CONFIG)  # Hermes writes it on first run
    before = snapshot(home)
    out = _install(home, dry_run=True)
    assert snapshot(home) == before
    for label in ("Claude Code", "Codex", "Cursor", "Gemini CLI", "Windsurf", "Devin",
                  "OpenCode", "GitHub Copilot", "Hermes Agent", "Antigravity"):  # fmt: skip
        assert f"== {label}" in out, label
    assert "skipped" not in out  # nothing in a fresh home needs a hand edit
    assert "inactive until you approve it" in out
