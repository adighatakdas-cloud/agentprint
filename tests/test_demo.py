"""The README demo: demo/build_demo.py builds its fake world, and every command that
demo/demo.tape shows works there and never prints a key in full."""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint.cli import app
from agentprint.ui.detect import AGENT_MARKERS

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


def test_tape_shows_scan_then_fix_then_pack():
    # the story: scan ends on BLOCK, fix (answered y) ends on CLEAR, pack shows the agent's view
    assert [c.split()[1] for c in COMMANDS] == ["scan", "fix", "pack"]


def test_tape_tells_block_then_fix_then_clear():
    tape = TAPE.read_text(encoding="utf-8")
    block = tape.index("Wait+Screen /BLOCK/")
    fix = tape.index('Type "agentprint fix')
    plan = tape.index("Wait+Screen /Apply 5 fixes/")
    answer = tape.index('Type "y"')
    clear = tape.index("Wait+Screen /CLEAR/")
    assert block < fix < plan < answer < clear
    assert tape.index('Type "agentprint pack') > clear


def test_tape_is_short_enough_for_a_readme():
    """Fixed pauses (Sleep) stay within the 40 s budget, leaving room for typing and Waits."""
    total = 0.0
    for line in TAPE.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*Sleep (\d+(?:\.\d+)?)(ms|s)", line)
        if match:
            total += float(match[1]) / (1000 if match[2] == "ms" else 1)
    assert total <= 33, total


def test_tape_records_a_first_run_not_an_agent_run():
    from agentprint.ui.detect import AGENT_MARKERS

    tape = TAPE.read_text(encoding="utf-8")
    for name in (*AGENT_MARKERS, "CI", "GITHUB_ACTIONS"):
        assert re.search(rf"\bunset\b[^`\n]*\b{name}\b", tape), name
    assert ".agentprint" in tape  # the welcome must not count as already seen


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def world(tmp_path, monkeypatch) -> Path:
    """A fresh demo world per command: `fix` changes it, and the tape shows commands in order."""
    root = tmp_path / "agentprint-demo"
    result = subprocess.run(
        [sys.executable, str(DEMO / "build_demo.py"), str(root)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # The GIF's world lives in /tmp, which other users can reach. pytest's temp folders are
    # private, so only look at the folders inside the world, as AP008 does for /tmp.
    from agentprint.checks import ap008_readable_by_others as ap008

    inside = root.resolve()
    monkeypatch.setattr(
        ap008, "_parents", lambda path: [p for p in path.parents if p.is_relative_to(inside)]
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("HOME", str(root))  # as the tape does: no real agent folder is read
    monkeypatch.setenv("COLUMNS", "100")
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME"):
        monkeypatch.delenv(name, raising=False)
    return root


def _run(command: str):
    args = shlex.split(command)[1:]
    if args[0] == "fix":
        # In the GIF a person types `y` at "Apply 5 fixes?". The test runner has no terminal,
        # so --yes is the same answer and --format table the same screen.
        args += ["--yes", "--format", "table"]
    return CliRunner().invoke(app, args)


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
def test_tape_command_runs_and_redacts(command, world):
    home = world / "fake-home"
    verb = command.split()[1]
    if verb == "pack":  # the tape runs fix first: pack sees the world after it
        assert _run(next(c for c in COMMANDS if c.split()[1] == "fix")).exit_code == 0
    before = _snapshot(home)

    result = _run(command)

    _no_full_key(result.output)
    if verb == "scan":
        assert result.exit_code == 1
        for check in ("AP001", "AP002", "AP003", "AP005"):
            assert check in result.output
        assert "keys masked" in result.output
        assert "BLOCK" in result.output
        assert _snapshot(home) == before  # a scan changes nothing
    elif verb == "fix":
        assert result.exit_code == 0, result.output
        assert "5 fixes ready" in result.output
        assert "CLEAR" in result.output and "BLOCK" not in result.output
        assert "Rotate the OpenAI API key" in result.output  # what only the human can do
        assert "FAKEFAKE" not in (home / ".claude" / "history.jsonl").read_text()
    elif verb == "pack":
        assert result.exit_code == 1
        assert result.output.startswith("agentprint: BLOCK")
        assert "AP006" in result.output and "ask_human:" in result.output
        assert "settings.local.json" in result.output
        assert _snapshot(home) == before
    else:
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output and "Claude Code" in result.output
        assert _snapshot(home) == before


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


# --- the agent-in-the-loop demo: demo/agent_loop.py and demo/agent_loop.tape ---------------

LOOP = DEMO / "agent_loop.py"
LOOP_TAPE = DEMO / "agent_loop.tape"


def test_loop_tape_runs_the_loop_script_and_is_short_enough():
    tape = LOOP_TAPE.read_text(encoding="utf-8")
    assert "Output docs/agent-loop.gif" in tape
    shown, hidden = [], False
    for line in tape.splitlines():
        word = line.strip().split(" ", 1)[0]
        hidden = {"Hide": True, "Show": False}.get(word, hidden)
        if word == "Type" and not hidden:
            shown.append(line.strip())
    assert shown == ['Type `python3 "$REPO/demo/agent_loop.py" "$DEMO"`']
    # the script's own pauses plus the tape's: the whole recording stays within a minute
    pauses = sum(
        float(n) for n in re.findall(r"^\s+pause\((\d+(?:\.\d+)?)\)", LOOP.read_text(), re.M)
    )
    sleeps = sum(
        float(m[1]) / (1000 if m[2] == "ms" else 1)
        for m in re.finditer(r"^\s*Sleep (\d+(?:\.\d+)?)(ms|s)", tape, re.M)
    )
    assert pauses + sleeps <= 50, (pauses, sleeps)
    for name in (*AGENT_MARKERS, "CI"):
        assert re.search(rf"\bunset\b[^`\n]*\b{name}\b", tape), name


def test_loop_script_labels_itself_simulated():
    assert "simulated agent · real agentprint output" in LOOP.read_text(encoding="utf-8")


def test_workflow_takes_a_tape_input_without_expressions_in_run():
    text = (ROOT / ".github" / "workflows" / "demo-gif.yml").read_text(encoding="utf-8")
    assert "demo/agent_loop.tape" in text and "demo/demo.tape" in text
    for block in re.findall(r"run: \|?\n?((?:\s+.*\n)+?)(?=\s*-|\Z)", text):
        assert "${{" not in block


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_loop_goes_block_then_fix_then_allowed(tmp_path):
    root = tmp_path / "agentprint-demo"
    built = subprocess.run(
        [sys.executable, str(DEMO / "build_demo.py"), str(root)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert built.returncode == 0, built.stderr
    env = {
        **os.environ,
        "HOME": str(root),
        "AGENT_LOOP_SPEED": "0",
        "AGENTPRINT_CMD": str(Path(sys.executable).parent / "agentprint"),
    }
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME", "CI", "CLAUDECODE"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, str(LOOP), str(root)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    out = result.stdout
    assert result.returncode == 0, result.stdout + result.stderr
    _no_full_key(out + result.stderr)
    banner = out.index("simulated agent")
    block = out.index("agentprint: BLOCK")
    fix = out.index("agent: running (in my-app): agentprint fix --pack --yes --format agent")
    fixed = out.index("agentprint fix: CLEAR")
    home_fix = out.index("agentprint fix --yes --format agent --home fake-home my-app")
    retry = out.index("agent: running again: npm publish")
    allowed = out.index("agent: the hook let it run this time")
    left = out.index("agent: Left for you: rotate these 3 keys.")
    assert banner < block < fix < fixed < home_fix < retry < allowed < left
    assert "sk-p…(len 56)" in out  # keys appear only as redacted stubs
    # The count and the keys come from the real fix answers' "Rotate ..." lines.
    told = re.findall(r'Rotate the (.+?) that starts ("[^"]*")', out[:left])
    assert len(set(told)) == 3
    for name, stub in told:
        assert f"agent:   - {name} {stub}" in out[left:]
    # Nothing the script wrote itself passes for agentprint output: every such line is dim.
    for line in out.splitlines():
        if "agent:" in line or "hook:" in line or "(shortened)" in line:
            assert line.startswith(("\033[2;36magent: ", "\033[2m")), line
    assert "CLEAR: allowed" not in out and "has told you which" not in out
    assert "Tell the human" not in out and "(shortened)" in out  # the cut is said out loud
    # only real agentprint commands changed the world: fix --pack wrote the ignore lines
    assert "/.claude/settings.local.json" in (root / "my-app" / ".npmignore").read_text()
    assert "FAKEFAKE" not in (root / "my-app" / ".claude" / "settings.local.json").read_text()
    assert "wrote .npmignore" not in out
    assert "FAKEFAKE" not in (root / "fake-home" / ".claude" / "history.jsonl").read_text()


def _loop_module():
    spec = importlib.util.spec_from_file_location("agent_loop", LOOP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("answer", "shortened"),
    [
        ("agentprint fix: CLEAR\nask_human:\n  - x\nTell the human each ask_human item.\n", True),
        ("agentprint fix: CLEAR\nask_human:\n  - x\n", False),
    ],
    ids=["cut", "whole"],
)
def test_loop_says_when_it_shortens_a_fix_answer(tmp_path, monkeypatch, capsys, answer, shortened):
    loop = _loop_module()
    monkeypatch.setattr(loop, "SPEED", 0)
    done = subprocess.CompletedProcess([], 0, stdout=answer, stderr="")
    monkeypatch.setattr(loop, "agentprint", lambda *args, **kwargs: done)
    assert loop.run_fix("agentprint fix", ["fix"], tmp_path) == answer
    out = capsys.readouterr().out
    assert "Tell the human" not in out
    assert ("(shortened)" in out) is shortened
    assert out.startswith("\033[2;36magent: running (in ")


def test_loop_counts_each_key_to_rotate_once():
    loop = _loop_module()
    first = '- AP002: Rotate the OpenAI API key that starts "sk-p…(len 56)": revoke it'
    second = (
        '- AP001: Rotate the Anthropic API key that starts "sk-a…(len 105)": revoke it\n'
        '- AP005: Rotate the OpenAI API key that starts "sk-p…(len 56)": revoke it'
    )
    assert loop.keys_to_rotate(first, second) == [
        'OpenAI API key "sk-p…(len 56)"',
        'Anthropic API key "sk-a…(len 105)"',
    ]
    assert loop.keys_to_rotate("agentprint fix: CLEAR — nothing to do") == []
