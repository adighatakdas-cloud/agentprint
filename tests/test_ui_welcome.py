import io
import os
import random
import stat

import pytest
from rich.console import Console
from typer.testing import CliRunner

from agentprint import __version__
from agentprint.cli import app
from agentprint.roster import AgentState
from agentprint.ui import sweep
from agentprint.ui.welcome import mark_welcome, seen_welcome, welcome_lines

SPEC = """\
   █
▄█████▄   agentprint 0.1.0
██▄▀▄██   The security check your AI coding agent runs for you.
▀▄███▄▀   ~/work/my-app · offline · no telemetry

          2 of 4 agents armed · Cursor, Gemini CLI are not

          › agentprint scan      sweep this machine for prints
          › agentprint pack      preflight a package before it ships
          › agentprint install   arm every agent"""

STATES = [
    AgentState("claude-code", "Claude Code", True, True),
    AgentState("codex", "Codex", True, True),
    AgentState("cursor", "Cursor", True, False),
    AgentState("gemini", "Gemini CLI", True, False),
]


def _lines():
    return welcome_lines(STATES, "0.1.0", "~/work/my-app")


def _final():
    return "\n".join(line.plain for line in _lines())


def test_welcome_text_matches_spec():
    assert _final() == SPEC


def test_armed_word_is_amber():
    summary = _lines()[5]
    spans = [(summary.plain[s.start : s.end], s.style) for s in summary.spans]
    assert ("armed", "ap.amber") in spans


def test_first_frame_is_noise_last_is_final():
    final = _final()
    got = list(sweep.frames(_lines(), rng=random.Random(0)))
    first = got[0].plain.split("\n")
    for row, want in zip(first, final.split("\n"), strict=True):
        assert len(row) == len(want)
        for a, b in zip(row, want, strict=True):
            if b == " ":
                assert a == " "
            else:
                assert a in sweep.GLYPHS or a == b  # edge columns are glyphs too
    assert got[0].plain != final
    assert got[-1].plain == final


def test_frame_count_is_30():
    assert len(list(sweep.frames(_lines(), rng=random.Random(0)))) == 30


def test_sweep_time_budget(monkeypatch):
    delays = []
    monkeypatch.setattr(sweep.time, "sleep", delays.append)
    console = Console(file=io.StringIO(), force_terminal=True, width=100)
    sweep.play(console, _lines(), animate=True)
    assert len(delays) == 30 and sum(delays) <= 1.3


def test_play_without_animation_prints_final_once():
    buf = io.StringIO()
    sweep.play(Console(file=buf, width=100), _lines(), animate=False)
    assert buf.getvalue().rstrip("\n") == _final()


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_mark_welcome_mode_0600(tmp_path):
    assert not seen_welcome(tmp_path)
    mark_welcome(tmp_path)
    assert seen_welcome(tmp_path)
    folder = tmp_path / ".agentprint"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "welcomed").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_mark_welcome_unwritable_home_does_not_raise(tmp_path):
    tmp_path.chmod(0o500)
    try:
        mark_welcome(tmp_path)
        assert not seen_welcome(tmp_path)
    finally:
        tmp_path.chmod(0o700)


def test_welcome_command_without_tty_prints_final_once(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    r = CliRunner().invoke(app, ["welcome", "--home", str(tmp_path)])
    assert r.exit_code == 0
    assert f"agentprint {__version__}" in r.output
    assert "\x1b[" not in r.output
    assert r.output.count("The security check") == 1
    assert seen_welcome(tmp_path)


def test_welcome_narrow_console_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep.time, "sleep", lambda s: None)
    console = Console(file=io.StringIO(), force_terminal=True, width=40)
    sweep.play(console, _lines(), animate=True)
    sweep.play(console, _lines(), animate=False)


def test_welcome_command_narrow_width(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    r = CliRunner().invoke(app, ["welcome", "--home", str(tmp_path), "--plain"])
    assert r.exit_code == 0


def test_welcome_interrupt_exits_130(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("agentprint.ui.sweep.play", boom)
    r = CliRunner().invoke(app, ["welcome", "--home", str(tmp_path)])
    assert r.exit_code == 130
    assert "Traceback" not in r.output


HOSTILE = "evil\x1b]0;pwned\x07\x1b]52;c;ZXZpbA==\x07\x1b[2Jdir"


def test_hostile_folder_name_is_cleaned_in_the_header():
    from agentprint.ui.welcome import header_lines

    text = "\n".join(line.plain for line in header_lines("0.1.0", f"~/{HOSTILE}"))
    assert "\x1b" not in text and "\x07" not in text
    assert "pwned" not in text and "ZXZpbA" not in text
    assert "~/evildir · offline" in text


@pytest.mark.skipif(os.name != "posix", reason="ESC in a folder name")
def test_welcome_in_a_hostile_folder_writes_no_escape_sequence(tmp_path, monkeypatch):
    folder = tmp_path / HOSTILE
    folder.mkdir()
    monkeypatch.chdir(folder)
    r = CliRunner().invoke(app, ["welcome", "--home", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "\x1b" not in r.output and "pwned" not in r.output and "ZXZpbA" not in r.output


def test_menu_header_cleans_the_folder_name():
    from agentprint.ui import menu

    text = "\n".join(line.plain for line in menu.intro_lines([], HOSTILE))
    assert "\x1b" not in text and "pwned" not in text
