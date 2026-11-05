"""Regressions from the independent review of install / uninstall. Fake homes only."""

import json
import tomllib
from pathlib import Path

import pytest

from agentprint import install
from agentprint.integrations import claude_code

QUIET = lambda line: None  # noqa: E731


def _launch(monkeypatch, *argv):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: list(argv))


def _install(home, names=None, dry_run=False, echo=QUIET):
    install.install(home, {}, names, dry_run, echo)


def _uninstall(home, names=None, dry_run=False, echo=QUIET):
    install.uninstall(home, {}, names, dry_run, echo)


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(p.relative_to(root)): (None if p.is_dir() else p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "o3"\n')
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(json.dumps({"theme": "dark"}, indent=2) + "\n")
    return home


# ---- 1. re-install, then uninstall, must keep the user's edits made between installs --------


def test_reinstall_then_uninstall_keeps_edits_made_between_installs(home, monkeypatch):
    config, settings = home / ".codex" / "config.toml", home / ".gemini" / "settings.json"
    _launch(monkeypatch, "/usr/local/bin/uvx", "agentprint")
    _install(home)
    config.write_text(config.read_text() + '\n[mcp_servers.github]\ncommand = "gh-mcp"\n')
    data = json.loads(settings.read_text())
    data["mcpServers"]["mine"] = {"command": "x"}
    settings.write_text(json.dumps(data, indent=2) + "\n")
    _launch(monkeypatch, "/Users/u/.local/bin/agentprint")  # later: a permanent install
    _install(home)
    _uninstall(home)
    toml = tomllib.loads(config.read_text())
    assert toml["mcp_servers"] == {"github": {"command": "gh-mcp"}} and toml["model"] == "o3"
    assert json.loads(settings.read_text()) == {
        "theme": "dark",
        "mcpServers": {"mine": {"command": "x"}},
    }
    assert "agentprint" not in config.read_text() + settings.read_text()


def test_new_instructions_version_then_uninstall_keeps_user_edits(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    rules = home / ".claude" / "CLAUDE.md"
    rules.write_text("# mine\n")
    _launch(monkeypatch, "/opt/agentprint")
    _install(home, ["claude-code"])
    rules.write_text(rules.read_text() + "- added between installs\n")
    monkeypatch.setattr(claude_code, "INSTRUCTIONS", "## agentprint v2 note\n- new text\n")
    _install(home, ["claude-code"])
    assert "agentprint v2 note" in rules.read_text()
    _uninstall(home)
    assert rules.read_text() == "# mine\n- added between installs\n"


def test_plain_round_trip_is_still_byte_identical(home, monkeypatch):
    (home / ".gemini" / "settings.json").write_text('{\n    "theme": "dark"}')  # odd format
    before = snapshot(home)
    _launch(monkeypatch, "/opt/agentprint")
    _install(home)
    _install(home)
    _uninstall(home)
    assert snapshot(home) == before
