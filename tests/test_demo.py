"""The README demo: demo/build_demo.py builds its fake world, and every command that
demo/demo.tape shows works there and never prints a key in full."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint.cli import app

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
TAPE = DEMO / "demo.tape"
pytestmark = pytest.mark.skipif(not TAPE.is_file(), reason="demo/ is not in the sdist")


def _shown_commands() -> list[str]:
    """`agentprint ...` commands the tape types while recording (not the hidden setup)."""
    if not TAPE.is_file():
        return []
    commands, hidden = [], False
    for line in TAPE.read_text(encoding="utf-8").splitlines():
        word = line.strip().split(" ", 1)[0]
        hidden = {"Hide": True, "Show": False}.get(word, hidden)
        typed = re.fullmatch(r'Type "(agentprint [^"]*)"', line.strip())
        if typed and not hidden:
            commands.append(typed.group(1))
    return commands


COMMANDS = _shown_commands()


def _no_full_key(output: str) -> None:
    # Every demo key has a "FAKEFAKE..." body: any run of it means a key was shown unredacted.
    assert "FAKEFAKE" not in output, output


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("agentprint-demo")
    result = subprocess.run(
        [sys.executable, str(DEMO / "build_demo.py"), str(root)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    _no_full_key(result.stdout + result.stderr)
    return root


def test_build_script_makes_the_fake_world(demo):
    assert (demo / "fake-home" / ".claude" / "history.jsonl").is_file()
    assert (demo / "my-app" / ".claude" / "settings.local.json").is_file()
    assert "FAKEFAKE" in (demo / "my-app" / ".claude" / "settings.local.json").read_text()


def test_build_script_refuses_a_folder_it_did_not_make(tmp_path):
    (tmp_path / "notes.txt").write_text("mine\n")
    result = subprocess.run(
        [sys.executable, str(DEMO / "build_demo.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]


def test_tape_shows_scan_then_pack():
    # ends on the BLOCK verdict an AI agent gets: the strongest last frame
    assert [c.split()[1] for c in COMMANDS] == ["scan", "pack"]


def test_tape_records_a_first_run_not_an_agent_run():
    from agentprint.ui.detect import AGENT_MARKERS

    tape = TAPE.read_text(encoding="utf-8")
    for name in (*AGENT_MARKERS, "CI", "GITHUB_ACTIONS"):
        assert re.search(rf"\bunset\b[^`\n]*\b{name}\b", tape), name
    assert ".agentprint" in tape  # the welcome must not count as already seen


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            c,
            marks=[
                pytest.mark.integration,
                pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed"),
            ],
        )
        if c.split()[1] == "pack"
        else c
        for c in COMMANDS
    ],
)
def test_tape_command_runs_and_redacts(command, demo, monkeypatch):
    monkeypatch.chdir(demo)
    monkeypatch.setenv("HOME", str(demo))  # as the tape does: no real agent folder is read
    monkeypatch.setenv("COLUMNS", "100")
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME"):
        monkeypatch.delenv(name, raising=False)
    home = demo / "fake-home"
    before = _snapshot(home)

    result = CliRunner().invoke(app, shlex.split(command)[1:])

    _no_full_key(result.output)
    verb = command.split()[1]
    if verb == "scan":
        assert result.exit_code == 1
        for check in ("AP001", "AP002", "AP003", "AP005"):
            assert check in result.output
        assert "keys masked" in result.output
    elif verb == "pack":
        assert result.exit_code == 1
        assert result.output.startswith("agentprint: BLOCK")
        assert "AP006" in result.output and "ask_human:" in result.output
        assert "Rotate the OpenAI API key" in result.output
    else:
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output and "Claude Code" in result.output
    assert _snapshot(home) == before  # nothing in the fake home was changed


def test_tape_menu_keys_land_on_roster():
    """Step 2 moves down the menu to Roster and picks it: in step with menu.ITEMS."""
    from agentprint.ui import menu

    lines = [line.strip() for line in TAPE.read_text(encoding="utf-8").splitlines()]
    bare = [i for i, line in enumerate(lines) if line == 'Type "agentprint"']
    assert len(bare) == 2  # the welcome, then the menu
    keys: list[str] = []
    for line in lines[bare[1] + 1 :]:
        word, _, count = line.partition(" ")
        if word in ("Up", "Down", "Enter"):
            keys += [word] * (int(count) if count.strip().isdigit() else 1)
        if keys.count("Enter") == 2:  # the first runs agentprint, the second picks the row
            break
    moves = keys[1:-1]
    row = sum(1 if key == "Down" else -1 for key in moves) % len(menu.ITEMS)
    assert keys[0] == keys[-1] == "Enter" and moves
    assert menu.ITEMS[row][1] == "Roster"
