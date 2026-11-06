"""Every style the human screens use must resolve in the theme.

rich resolves a theme name only when the whole style string is that name: "bold ap.amber"
fails to parse, and Text.render then silently drops the style. These tests look every style
up with console.get_style() WITHOUT a default, so an unknown or compound name raises.
"""

import io
import random
import re

import pytest
from rich.console import Console
from rich.style import Style
from rich.text import Text

from agentprint.models import Finding, Severity
from agentprint.report import FailOn
from agentprint.roster import AgentState
from agentprint.secrets import redact
from agentprint.ui import menu, sweep
from agentprint.ui.results import render
from agentprint.ui.theme import console as themed_console
from agentprint.ui.theme import theme
from agentprint.ui.welcome import header_lines, welcome_lines
from tests import fakes

STATES = [
    AgentState("claude-code", "Claude Code", True, True),
    AgentState("cursor", "Cursor", True, False),
]


def _styles(text: Text) -> list:
    return ([text.style] if text.style else []) + [span.style for span in text.spans]


class _Recorder(Console):
    """A themed console that keeps every Text it is asked to print."""

    def __init__(self) -> None:
        super().__init__(file=io.StringIO(), width=100, theme=theme({}), highlight=False)
        self.texts: list[Text] = []

    def print(self, *objects, **kwargs) -> None:  # type: ignore[override]
        self.texts += [o for o in objects if isinstance(o, Text)]
        super().print(*objects, **kwargs)


def _strict(texts) -> int:
    """Look every style up without a default; return how many were checked."""
    con = themed_console({}, file=io.StringIO())
    checked = 0
    for text in texts:
        for style in _styles(text):
            con.get_style(style)  # raises MissingStyle for anything the theme can't resolve
            checked += 1
    return checked


def test_guard_catches_a_compound_style():
    bad = Text("print", style="bold ap.amber")
    with pytest.raises(Exception, match="ap.amber"):
        _strict([bad])


def test_welcome_styles_resolve():
    lines = welcome_lines(STATES, "0.1.0", "~/work/my-app")
    assert _strict(lines) > 0
    frames = list(sweep.frames(lines, rng=random.Random(0)))
    assert _strict(frames) > 0


def test_menu_styles_resolve():
    assert _strict(menu.intro_lines(STATES, "~/work/my-app")) > 0
    for current in range(len(menu.ITEMS)):
        assert _strict([t for row in menu.rows(current) for t in row]) > 0


def test_results_styles_resolve():
    def f(check_id, sev, detail, count=1):
        return Finding(check_id, sev, "t", "claude-code", "/p/x.json", 1, detail, "fix", count)

    findings = [
        f("AP001", Severity.HIGH, f"key {redact(fakes.ANTHROPIC)}", count=2),
        f("AP005", Severity.CRITICAL, "token"),
        f("AP003", Severity.MEDIUM, "Bash(*)"),
        f("AP008", Severity.LOW, "mode 0644"),
    ]
    for fail_on in (FailOn.high, FailOn.none):
        for found in (findings, []):
            out = _Recorder()
            render(found, out, fail_on=fail_on, header="agentprint scan  ~ · 0.01s")
            assert _strict(out.texts) > 0


def test_status_styles_resolve(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentprint import cli

    (tmp_path / ".claude").mkdir()
    made: list[_Recorder] = []

    def recorder(env, file=None, plain=False, **kwargs):
        made.append(_Recorder())
        return made[-1]

    monkeypatch.setattr(cli, "themed_console", recorder)
    r = CliRunner().invoke(cli.app, ["status", "--home", str(tmp_path), "--format", "table"])
    assert r.exit_code == 0, r.output
    assert _strict(made[0].texts) > 0


# ---- the welcome header in real escape codes -------------------------------------------------

AMBER = "38;2;255;176;32"  # #FFB020


@pytest.fixture
def fresh_styles():
    """rich keeps a Style's escape codes from the first color system it was drawn in, and
    caches parsed and combined styles: an earlier 256-color test would leak into these."""
    for name in dir(Style):
        clear = getattr(getattr(Style, name, None), "cache_clear", None)
        if clear is not None:
            clear()


def _ansi_header() -> list[str]:
    buf = io.StringIO()
    con = themed_console({}, file=buf, force_terminal=True, color_system="truecolor", width=100)
    for line in header_lines("0.1.0", "~/work/my-app"):
        con.print(line)
    return buf.getvalue().splitlines()


def test_print_is_bold_amber_agent_is_bold(fresh_styles):
    name = _ansi_header()[1]
    assert f"\x1b[1;{AMBER}mprint" in name
    assert "\x1b[1magent" in name


def test_tagline_is_normal_text(fresh_styles):
    line = _ansi_header()[2]
    start = line.index("The security check")
    after_mascot = line[line.rindex("\x1b[0m", 0, start) + len("\x1b[0m") :]
    assert not re.search(r"\x1b\[", after_mascot), repr(after_mascot)  # no bold, no color


def test_last_sweep_frame_renders_like_the_final_lines(fresh_styles):
    lines = welcome_lines(STATES, "0.1.0", "~/work/my-app")
    last = list(sweep.frames(lines, rng=random.Random(0)))[-1]

    def ansi(*renderables) -> str:
        buf = io.StringIO()
        con = themed_console({}, file=buf, force_terminal=True, color_system="truecolor")
        for r in renderables:
            con.print(r, soft_wrap=True)
        return buf.getvalue()

    assert ansi(last) == ansi(*lines)
    assert f"\x1b[1;{AMBER}mprint" in ansi(last)
