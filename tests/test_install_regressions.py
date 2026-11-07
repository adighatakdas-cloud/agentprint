"""Regressions from the independent review of install / uninstall. Fake homes only."""

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from agentprint import install
from agentprint.integrations import claude_code
from agentprint.integrations.base import BlockEdit

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


@pytest.mark.posix_only
@pytest.mark.posix_only
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


@pytest.mark.posix_only
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


@pytest.mark.posix_only
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


# ---- 4. the diff shows agentprint's own lines only, never the user's other values -----------

PRIVATE = ("FAKEpassw0rd", "lin_api_FAKE", "db.internal", "pg-mcp")


def test_diff_never_shows_other_values_from_the_users_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    env = {
        "DATABASE_URL": "postgres://admin:FAKEpassw0rd@db.internal:5432/prod",
        "LINEAR_TOKEN": "lin_api_FAKEFAKEFAKEFAKE",
    }
    compact = json.dumps({"mcpServers": {"db": {"command": "pg-mcp", "env": env}}}) + "\n"
    (home / ".cursor" / "mcp.json").write_text(compact)  # compact: re-indenting touches every line
    (home / ".cursor" / "rules.md").write_text("x")
    _launch(monkeypatch, "/opt/agentprint")
    for run in (
        lambda out: _install(home, ["cursor"], dry_run=True, echo=out.append),
        lambda out: _install(home, ["cursor"], echo=out.append),
        lambda out: _uninstall(home, ["cursor"], echo=out.append),
    ):
        out: list[str] = []
        run(out)
        text = "\n".join(out)
        assert not any(secret in text for secret in PRIVATE), text
        assert '"agentprint"' in text  # agentprint's own entry is still shown
    assert (home / ".cursor" / "mcp.json").read_text() == compact


# ---- 5 / 13. marked blocks: no joined lines, foreign TOML inside the block survives ----------


def _codex_home(tmp_path, config: str) -> Path:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(config)
    return home


def test_toml_without_final_newline_stays_valid_when_codex_appends(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, '[profiles.x]\nmodel = "o3"')
    config = home / ".codex" / "config.toml"
    _launch(monkeypatch, "/opt/agentprint")
    _install(home, ["codex"])
    text = config.read_text()
    text += "" if text.endswith("\n") else "\n"  # what a TOML writer does before a new table
    config.write_text(text + '[projects."/x"]\ntrust_level = "trusted"\n')
    tomllib.loads(config.read_text())
    _uninstall(home, ["codex"])
    data = tomllib.loads(config.read_text())
    assert data == {
        "profiles": {"x": {"model": "o3"}},
        "projects": {"/x": {"trust_level": "trusted"}},
    }


@pytest.mark.parametrize("style", ["md", "toml"])
def test_block_at_end_of_unterminated_file_keeps_appended_line_apart(tmp_path, style):
    # The reviewer's case: original has no final newline, something is appended after the block.
    edit = BlockEdit(tmp_path / "f", "x", "body line\n", style)
    installed = edit.apply('model = "o3"')
    reverted = edit.revert(installed + '[projects."/x"]\n')
    assert reverted == 'model = "o3"\n[projects."/x"]\n'
    assert edit.revert(installed) == 'model = "o3"'  # nothing appended: exact original
    assert edit.revert(edit.apply("a\n")) == "a\n"
    assert edit.revert(edit.apply("a\n") + "b\n") == "a\nb\n"


def test_markdown_without_final_newline_keeps_lines_apart(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    rules = home / ".claude" / "CLAUDE.md"
    rules.write_text("# mine")
    _launch(monkeypatch, "/opt/agentprint")
    _install(home, ["claude-code"])
    rules.write_text(rules.read_text() + "- added later\n")
    _uninstall(home)
    assert rules.read_text() == "# mine\n- added later\n"


def test_codex_server_block_sits_above_root_keys_and_tables(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, 'model = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n')
    _launch(monkeypatch, "/opt/agentprint")
    _install(home, ["codex"])
    text = (home / ".codex" / "config.toml").read_text()
    assert text.startswith("# agentprint:start")
    data = tomllib.loads(text)
    assert data["model"] == "o3"
    assert data["mcp_servers"] == {
        "agentprint": {"command": "/opt/agentprint", "args": ["mcp"]},
        "other": {"command": "x"},
    }


def test_tables_codex_writes_inside_our_block_are_kept(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, 'model = "o3"\n')
    config = home / ".codex" / "config.toml"
    _launch(monkeypatch, "/opt/agentprint")
    _install(home, ["codex"])
    # Suppose Codex's TOML writer puts new entries just before our end marker.
    text = config.read_text().replace(
        "# agentprint:end", 'mcp_servers.github.command = "gh-mcp"\n# agentprint:end'
    )
    config.write_text(text)
    _uninstall(home, ["codex"])
    data = tomllib.loads(config.read_text())
    assert data == {"model": "o3", "mcp_servers": {"github": {"command": "gh-mcp"}}}
    assert "agentprint" not in config.read_text()


# ---- 9. file permissions: private temp files, modes kept, read-only files left alone --------


@pytest.mark.posix_only
def test_read_only_file_is_not_edited(tmp_path, monkeypatch):
    home = _claude_home(tmp_path)
    settings = home / ".claude" / "settings.json"
    settings.chmod(0o444)
    _launch(monkeypatch, "/opt/agentprint")
    out: list[str] = []
    _install(home, echo=out.append)
    assert settings.read_text() == "{}\n" and stat.S_IMODE(settings.stat().st_mode) == 0o444
    assert any("read-only" in line for line in out) and any("by hand" in line for line in out)


@pytest.mark.posix_only
def test_temp_files_are_private_from_the_first_byte_and_modes_are_kept(tmp_path, monkeypatch):
    home = _claude_home(tmp_path)
    state = home / ".claude.json"
    state.write_text("{}\n")
    state.chmod(0o640)
    opened: list[tuple[str, int, int]] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        opened.append((str(path), flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(install.os, "open", spy)
    _launch(monkeypatch, "/opt/agentprint")
    _install(home)
    temps = [(flags, mode) for path, flags, mode in opened if "agentprint-tmp" in path]
    assert temps and all(mode == 0o600 and flags & os.O_EXCL for flags, mode in temps)
    assert stat.S_IMODE(state.stat().st_mode) == 0o640
    assert "agentprint" in state.read_text()
    launcher = home / ".agentprint" / "bin" / "agentprint-hook"
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o700  # runnable by its owner only


def test_crlf_line_endings_are_kept(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    rules = home / ".claude" / "CLAUDE.md"
    rules.write_bytes(b"# mine\r\n- keep\r\n")
    _launch(monkeypatch, "/opt/agentprint")
    _install(home, ["claude-code"])
    assert rules.read_bytes().startswith(b"# mine\r\n- keep\r\n")
    _uninstall(home)
    assert rules.read_bytes() == b"# mine\r\n- keep\r\n"


# ---- 11. how hooks start agentprint ----------------------------------------------------------


def test_uvx_fallback_is_pinned_to_this_version(monkeypatch):
    from agentprint import __version__

    found = {"agentprint": None, "uvx": "/Users/u/.local/bin/uvx"}
    monkeypatch.setattr(install.shutil, "which", lambda name: found[name])
    expected = ["/Users/u/.local/bin/uvx", f"agentprint=={__version__}"]
    assert install.resolve_agentprint() == expected


@pytest.mark.parametrize(
    ("path", "ephemeral"),
    [
        ("/Users/u/.cache/uv/archive-v0/abc/bin/agentprint", True),
        ("/Users/u/.local/pipx/.cache/0f1e2d/bin/agentprint", True),
        ("/Users/u/Library/Caches/pipx/0f1e2d/bin/agentprint", True),
        ("/home/u/.cache/pipx/0f1e2d/bin/agentprint", True),
        ("/tmp/xyz/bin/agentprint", True),
        ("/Users/u/.local/bin/agentprint", False),
        ("/Users/u/.local/pipx/venvs/agentprint/bin/agentprint", False),
        ("/Users/u/.local/share/uv/tools/agentprint/bin/agentprint", False),
    ],
)
def test_throwaway_installs_are_never_written_into_hooks(path, ephemeral):
    assert install._ephemeral(path) is ephemeral


# ---- 15. Devin Desktop keeps ~/.devin; Devin CLI config lives in ~/.config/devin ----------


@pytest.mark.parametrize("existing", [".devin", ".config/devin"])
def test_devin_is_found_by_either_folder_and_round_trips(tmp_path, monkeypatch, existing):
    home = tmp_path / "home"
    (home / existing).mkdir(parents=True)
    before = snapshot(home)
    _launch(monkeypatch, "/opt/agentprint")
    out: list[str] = []
    _install(home, echo=out.append)
    assert any("Devin" in line for line in out if line.startswith("Agents found"))
    hooks = json.loads((home / ".config" / "devin" / "config.json").read_text())
    assert hooks["hooks"]["PreToolUse"][0]["matcher"] == "^exec$"
    mcp = json.loads((home / ".config" / "devin" / "mcp_config.json").read_text())
    assert mcp["mcpServers"]["agentprint"]["command"] == "/opt/agentprint"
    rule = (home / ".devin" / "rules" / "agentprint.md").read_text()
    assert rule.startswith("---\ntrigger: always_on\n---\n") and "agentprint: BLOCK" in rule
    _uninstall(home)
    assert snapshot(home) == before
