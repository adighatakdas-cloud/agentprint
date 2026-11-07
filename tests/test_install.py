"""install / uninstall. Every test uses a fake home under tmp_path: never the real one."""

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint import install
from agentprint.cli import app
from tests import fakes

# POSIX-only: asserts the /bin/sh launcher, hook command strings and file modes (Windows installs
# only the MCP server and the note; see tests/test_windows_gate.py).
pytestmark = pytest.mark.posix_only

runner = CliRunner()
AGENTPRINT = ["/opt/agentprint/bin/agentprint"]


@pytest.fixture(autouse=True)
def fixed_launch(monkeypatch):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: list(AGENTPRINT))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    claude_settings = {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"command": "echo hi"}]}]},
    }
    _write(home / ".claude" / "settings.json", json.dumps(claude_settings, indent=4))  # no \n
    state = {"numStartups": 3, "mcpServers": {"other": {"command": "x", "env": {}}}}
    state["mcpServers"]["other"]["env"]["TOKEN"] = fakes.GITHUB
    _write(home / ".claude.json", json.dumps(state, indent=2) + "\n")
    _write(home / ".claude" / "CLAUDE.md", "# My rules\n- be nice\n")
    _write(
        home / ".codex" / "config.toml", 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n'
    )
    (home / ".cursor").mkdir(parents=True)
    _write(home / ".gemini" / "settings.json", '{\n  "theme": "x"\n}\n')
    _write(home / ".codeium" / "windsurf" / "mcp_config.json", '{"mcpServers": {}}')
    _write(home / ".config" / "devin" / "config.json", '{\n  // my settings\n  "a": 1\n}\n')
    _write(home / ".config" / "opencode" / "opencode.json", '{"theme": "x"}\n')
    return home


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(p.relative_to(root)): (None if p.is_dir() else p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


def run(*args):
    return runner.invoke(app, list(args))


def test_dry_run_changes_nothing_but_shows_diffs(home):
    before = snapshot(home)
    result = run("install", "--home", str(home), "--dry-run")
    assert result.exit_code == 0, result.output
    assert snapshot(home) == before
    added = [line.strip() for line in result.output.splitlines() if line.strip().startswith("+")]
    assert any("agentprint" in line for line in added) and "dry run" in result.output.lower()


def test_install_wires_every_detected_agent(home):
    result = run("install", "--home", str(home))
    assert result.exit_code == 0, result.output

    claude = json.loads((home / ".claude" / "settings.json").read_text())
    groups = claude["hooks"]["PreToolUse"]
    assert groups[0]["matcher"] == "Edit"  # the user's own hook is kept
    assert groups[1]["matcher"] == "Bash"
    assert "agentprint-hook" in groups[1]["hooks"][0]["command"]
    assert "--agent claude-code" in groups[1]["hooks"][0]["command"]
    assert claude["permissions"] == {"allow": ["Bash(ls)"]}

    state = json.loads((home / ".claude.json").read_text())
    assert state["mcpServers"]["agentprint"]["args"] == ["mcp"]
    assert state["mcpServers"]["agentprint"]["command"] == AGENTPRINT[0]
    assert "other" in state["mcpServers"] and state["numStartups"] == 3

    assert "agentprint:start" in (home / ".claude" / "CLAUDE.md").read_text()
    assert (home / ".claude" / "CLAUDE.md").read_text().startswith("# My rules\n- be nice\n")

    codex_hooks = json.loads((home / ".codex" / "hooks.json").read_text())
    assert codex_hooks["hooks"]["PreToolUse"][0]["matcher"] == "^Bash$"
    toml = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert toml["mcp_servers"]["agentprint"] == {"command": AGENTPRINT[0], "args": ["mcp"]}
    assert toml["mcp_servers"]["other"] == {"command": "x"} and toml["model"] == "gpt-5"
    assert "agentprint:start" in (home / ".codex" / "AGENTS.md").read_text()

    cursor = json.loads((home / ".cursor" / "hooks.json").read_text())
    assert cursor["version"] == 1 and cursor["hooks"]["beforeShellExecution"][0]["matcher"]
    assert json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["agentprint"]
    assert "alwaysApply: true" in (home / ".cursor" / "rules" / "agentprint.mdc").read_text()

    gemini = json.loads((home / ".gemini" / "settings.json").read_text())
    assert gemini["theme"] == "x"
    assert gemini["hooks"]["BeforeTool"][0]["matcher"] == "run_shell_command"
    assert gemini["mcpServers"]["agentprint"]["command"] == AGENTPRINT[0]
    assert (home / ".gemini" / "GEMINI.md").is_file()

    windsurf = json.loads((home / ".codeium" / "windsurf" / "hooks.json").read_text())
    assert "agentprint-hook" in windsurf["hooks"]["pre_run_command"][0]["command"]
    assert (home / ".codeium" / "windsurf" / "memories" / "global_rules.md").is_file()

    # Devin's config.json has comments: left alone, with the snippet to paste by hand.
    assert "// my settings" in (home / ".config" / "devin" / "config.json").read_text()
    assert "by hand" in result.output
    assert json.loads((home / ".config" / "devin" / "mcp_config.json").read_text())["mcpServers"]

    opencode = home / ".config" / "opencode"
    assert "agentprint-hook" in (opencode / "plugins" / "agentprint.js").read_text()
    assert json.loads((opencode / "opencode.json").read_text())["mcp"]["agentprint"]["type"] == (
        "local"
    )
    # ~/.claude/CLAUDE.md exists, so OpenCode's AGENTS.md is not created (it would hide it).
    assert not (opencode / "AGENTS.md").exists()

    launcher = home / ".agentprint" / "bin" / "agentprint-hook"
    assert launcher.stat().st_mode & stat.S_IXUSR
    assert "/opt/agentprint/bin/agentprint guard" in launcher.read_text()
    assert "/hooks" in result.output  # Codex trust reminder


def test_install_is_idempotent(home):
    assert run("install", "--home", str(home)).exit_code == 0
    first = snapshot(home)
    result = run("install", "--home", str(home))
    assert result.exit_code == 0
    assert snapshot(home) == first
    assert "already set up" in result.output.lower()


def test_install_then_uninstall_is_byte_identical(home):
    before = snapshot(home)
    assert run("install", "--home", str(home)).exit_code == 0
    assert snapshot(home) != before
    result = run("uninstall", "--home", str(home))
    assert result.exit_code == 0, result.output
    assert snapshot(home) == before


def test_uninstall_keeps_changes_made_after_install(home):
    run("install", "--home", str(home))
    path = home / ".claude" / "settings.json"
    data = json.loads(path.read_text())
    data["model"] = "opus"
    path.write_text(json.dumps(data, indent=4))
    md = home / ".claude" / "CLAUDE.md"
    md.write_text(md.read_text() + "\n- added later\n")
    assert run("uninstall", "--home", str(home)).exit_code == 0
    after = json.loads(path.read_text())
    assert after["model"] == "opus"
    assert [g["matcher"] for g in after["hooks"]["PreToolUse"]] == ["Edit"]
    text = md.read_text()
    assert "agentprint" not in text and "- added later" in text and "- be nice" in text


def test_uninstall_without_install_changes_nothing(home):
    before = snapshot(home)
    assert run("uninstall", "--home", str(home)).exit_code == 0
    assert snapshot(home) == before


def test_only_detected_agents_are_touched(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    assert run("install", "--home", str(home)).exit_code == 0
    assert (home / ".claude" / "settings.json").is_file()
    assert not (home / ".cursor").exists() and not (home / ".codex").exists()


def test_agent_option_limits_to_one_agent(home):
    before = snapshot(home)
    assert run("install", "--home", str(home), "--agent", "gemini").exit_code == 0
    changed = {k for k, v in snapshot(home).items() if before.get(k, b"missing") != v}
    assert changed and all(k.startswith((".gemini", ".agentprint")) for k in changed)


def test_unknown_agent_name_is_an_error(home):
    result = run("install", "--home", str(home), "--agent", "notepad")
    assert result.exit_code == 2


def test_output_never_shows_a_full_key(home):
    for args in (("install",), ("install", "--dry-run"), ("uninstall",)):
        result = run(*args, "--home", str(home))
        assert fakes.GITHUB not in result.output


def test_backups_are_private(home):
    run("install", "--home", str(home))
    backups = [p for p in (home / ".agentprint" / "backups").rglob("*") if p.is_file()]
    assert backups
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in backups)


def test_symlinked_rules_file_is_edited_in_place(tmp_path):
    home = tmp_path / "home"
    dotfiles = tmp_path / "dotfiles" / "CLAUDE.md"
    _write(dotfiles, "# shared\n")
    (home / ".claude").mkdir(parents=True)
    os.symlink(dotfiles, home / ".claude" / "CLAUDE.md")
    run("install", "--home", str(home))
    assert (home / ".claude" / "CLAUDE.md").is_symlink()
    assert "agentprint:start" in dotfiles.read_text()
    run("uninstall", "--home", str(home))
    assert dotfiles.read_text() == "# shared\n"


# Launcher exit codes and output per agent: tests/test_launcher.py.


def test_scan_name_gemini_cli_is_accepted_for_install(home):
    # Findings name the agent "gemini-cli"; an AI copying that into --agent must not fail.
    before = snapshot(home)
    result = run("install", "--home", str(home), "--agent", "gemini-cli")
    assert result.exit_code == 0, result.output
    changed = {k for k, v in snapshot(home).items() if before.get(k, b"missing") != v}
    assert changed and all(k.startswith((".gemini", ".agentprint")) for k in changed)


def test_setup_help_lists_every_agent_and_does_not_overclaim():
    from agentprint.integrations import ALL_INTEGRATIONS

    install_help = " ".join(run("install", "--help").output.split())
    for name in ALL_INTEGRATIONS:
        assert name in install_help
    uninstall_help = " ".join(run("uninstall", "--help").output.split())
    assert "exactly" not in uninstall_help


def test_relative_home_gives_hooks_an_absolute_launcher_path(home, monkeypatch):
    # Agents run hooks from any folder, so a relative launcher path would never be found.
    monkeypatch.chdir(home.parent)
    assert run("install", "--home", home.name, "--agent", "claude-code").exit_code == 0
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    [command] = [
        h["command"]
        for group in settings["hooks"]["PreToolUse"]
        for h in group["hooks"]
        if "agentprint-hook" in h["command"]
    ]
    assert str(home / ".agentprint" / "bin" / "agentprint-hook") in command
    assert " home/.agentprint" not in command
