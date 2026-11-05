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


# ---- 3. odd files never crash install halfway; state is always saved -------------------------


def _claude_home(tmp_path) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}\n")
    return home


@pytest.mark.parametrize(
    "gemini_settings",
    [
        b'{"theme": "caf\xe9"}\n',  # not UTF-8
        b'{"hooks": []}\n',  # hooks is a list
        b'{"hooks": {"BeforeTool": {}}}\n',  # event is an object
        b'{"mcpServers": []}\n',  # servers is a list
    ],
)
def test_odd_file_is_skipped_and_the_rest_installs(tmp_path, monkeypatch, gemini_settings):
    home = _claude_home(tmp_path)
    (home / ".gemini").mkdir()
    settings = home / ".gemini" / "settings.json"
    settings.write_bytes(gemini_settings)
    _launch(monkeypatch, "/opt/agentprint")
    out: list[str] = []
    _install(home, echo=out.append)
    assert settings.read_bytes() == gemini_settings  # left untouched
    assert any("by hand" in line for line in out)
    assert "agentprint-hook" in (home / ".claude" / "settings.json").read_text()
    assert (home / ".agentprint" / "state.json").is_file()
    _uninstall(home)
    assert settings.read_bytes() == gemini_settings


def test_lone_surrogate_escape_in_json_does_not_crash(tmp_path, monkeypatch):
    home = _claude_home(tmp_path)
    state = home / ".claude.json"
    original = '{\n  "projects": {"/x": {"history": [{"display": "hi \\ud83d"}]}}\n}\n'
    state.write_text(original)
    _launch(monkeypatch, "/opt/agentprint")
    _install(home, dry_run=True)
    _install(home)
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["mcpServers"]["agentprint"]["command"] == "/opt/agentprint"
    assert data["projects"]["/x"]["history"][0]["display"] == "hi \ud83d"
    _uninstall(home)
    assert state.read_text() == original


def test_unexpected_error_in_one_file_still_saves_state(tmp_path, monkeypatch):
    home = _claude_home(tmp_path)
    (home / ".codex").mkdir()
    _launch(monkeypatch, "/opt/agentprint")
    real_write = install._atomic_write

    def flaky(path, text, mode=None):
        if path.name == "hooks.json":
            raise PermissionError("disk said no")
        real_write(path, text, mode)

    monkeypatch.setattr(install, "_atomic_write", flaky)
    out: list[str] = []
    _install(home, echo=out.append)
    assert any("hooks.json" in line and "could not" in line for line in out)
    assert not (home / ".codex" / "hooks.json").exists()
    assert "agentprint-hook" in (home / ".claude" / "settings.json").read_text()
    assert (
        str(home / ".claude" / "settings.json")
        in json.loads((home / ".agentprint" / "state.json").read_text())["files"]
    )


# ---- 2. uninstall --agent NAME touches only that agent's files -------------------------------


def test_uninstall_one_agent_leaves_the_others(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}\n")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "o3"\n')
    _launch(monkeypatch, "/opt/agentprint")
    _install(home)
    kept = (".claude", ".agentprint/bin")
    claude_before = {k: v for k, v in snapshot(home).items() if k.startswith(kept)}
    _uninstall(home, ["codex"])
    after = snapshot(home)
    assert {k: after.get(k) for k in claude_before} == claude_before  # Claude Code untouched
    assert after[".codex/config.toml"] == b'model = "o3"\n'
    assert ".codex/hooks.json" not in after and ".codex/AGENTS.md" not in after
    _uninstall(home, ["claude-code"])  # the last agent: launcher and state go too
    assert not (home / ".agentprint").exists()
    assert (home / ".claude" / "settings.json").read_text() == "{}\n"
