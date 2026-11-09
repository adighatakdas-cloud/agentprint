"""The `agentprint fix` screens a person sees: columns line up, long text wraps under its own
column, and the counts add up. Plans are built by hand; no file is read or changed."""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from agentprint.fix import Change, Manual, Outcome, Plan
from agentprint.models import Finding, Severity
from agentprint.report import FailOn
from agentprint.secrets import redact
from agentprint.ui import fixview
from agentprint.ui.theme import theme
from tests import fakes



def _console(width: int) -> Console:
    return Console(file=io.StringIO(), width=width, theme=theme({}), no_color=True, highlight=False)


def _text(console: Console) -> list[str]:
    return console.file.getvalue().splitlines()  # type: ignore[attr-defined]


def _plan(*, folder_files: int = 3) -> Plan:
    home = Path.home()  # the test's own home (conftest): paths in it are shown as ~
    codex = home / ".codex"
    locks = [codex / f"f{i}.jsonl" for i in range(folder_files)]
    ap008 = Finding(
        "AP008", Severity.MEDIUM, "t", "codex", str(codex), None, "d (mode 0644)", "chmod", 3
    )
    ap008.paths = tuple(locks)
    changes = [
        Change("mask", home / ".claude" / "history.jsonl", 1, "AP001", redact(fakes.ANTHROPIC)),
        Change(
            "env",
            home / ".claude.json",
            10,
            "AP005",
            "shown",
            json_path='["mcpServers", "github", "env", "GITHUB_TOKEN"]',
            var="GITHUB_TOKEN",
        ),
        Change(
            "ignore",
            home / "code" / "app-that-does-not-exist" / ".gitignore",
            None,
            "AP007",
            "shown",
            line_text="/.claude/settings.local.json",
        ),
        *(Change("lock", p, None, "AP008", "mode", old_mode=0o644) for p in locks),
    ]
    manual = [
        Manual("AP001", f'Rotate the Anthropic API key that starts "{redact(fakes.ANTHROPIC)}".'),
        Manual("AP003", "Delete this entry from permissions.allow " + "and keep it narrow " * 8),
    ]
    return Plan(changes, manual, [ap008])


def test_rows_line_up_in_three_columns_when_they_fit():
    con = _console(120)
    fixview.render_plan(_plan(), con, header="agentprint fix  ~ + .")
    lines = _text(con)
    assert lines[0] == "agentprint fix  ~ + . · 4 fixes ready"
    fixing = lines[lines.index("Fixing") + 1 : lines.index("Fixing") + 5]
    assert [ln.split()[0] for ln in fixing] == ["mask", "env", "ignore", "lock"]
    assert {ln.index(ln.split()[1]) for ln in fixing} == {10}  # the place column
    whats = ["sk-a…", "github.env", "+ /.claude", "3 files"]
    assert len({ln.index(w) for ln, w in zip(fixing, whats, strict=True)}) == 1
    assert "~/.codex" in fixing[3] and "3 files → owner only" in fixing[3]


def test_rows_take_two_lines_when_one_would_not_fit():
    con = _console(60)
    fixview.render_plan(_plan(), con)
    lines = _text(con)
    fixing = lines[lines.index("Fixing") + 1 : lines.index("Fixing") + 9]
    places, whats = fixing[0::2], fixing[1::2]
    assert [ln.split()[0] for ln in places] == ["mask", "env", "ignore", "lock"]
    assert all(ln.startswith(" " * 10) and ln[10] != " " for ln in whats), whats
    assert whats[1].strip() == "github.env.GITHUB_TOKEN → ${GITHUB_TOKEN}"
    assert all(len(ln) <= 60 for ln in lines if not ln.startswith("  ignore"))


def test_needs_you_wraps_under_its_own_column():
    con = _console(60)
    fixview.render_plan(_plan(), con)
    lines = _text(con)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("  2  AP003  "))
    item = lines[start : lines.index("", start)]
    assert item[0].startswith("  2  AP003  Delete this entry")
    assert len(item) > 2 and all(ln.startswith(" " * 12) and ln[12] != " " for ln in item[1:])
    assert all(len(ln) <= 60 for ln in item)


def test_the_plan_says_masks_cannot_be_undone_and_ends_on_it():
    con = _console(100)
    fixview.render_plan(_plan(), con)
    lines = _text(con)
    assert "can't be undone" in " ".join(lines[-4:-1])
    assert lines[-1] == "agentprint fix --undo puts back ignore and lock."  # never split


def test_fixed_and_not_fixed_add_up_to_the_fixes_ready():
    plan = _plan()
    locks = [c for c in plan.changes if c.kind == "lock"]
    others = [c for c in plan.changes if c.kind != "lock"]
    outcome = Outcome(others, [(c, "the file changed") for c in locks], None)
    con = _console(100)
    fixview.render_outcome(outcome, [], con, FailOn.high, plan=plan)
    lines = _text(con)
    assert lines[0] == ""  # a blank line after the y/N question
    assert lines[1].startswith("3 fixed · 1 not fixed")  # 4 fixes ready: the 3 locks are one row
    assert lines[-1].startswith(" CLEAR ")


def test_still_blocking_never_calls_a_key_masked():
    left = Finding(
        "AP002",
        Severity.CRITICAL,
        "Secret in agent settings",
        "claude-code",
        str(Path.home() / ".claude" / "settings.json"),
        3,
        f"OpenAI API key: {redact(fakes.OPENAI)}",
        "fix",
    )
    con = _console(100)
    fixview.render_outcome(Outcome([], [], None), [left], con, FailOn.high, plan=_plan())
    lines = _text(con)
    shown = lines[lines.index("Still blocking") + 1 : lines.index("Still blocking") + 3]
    assert shown[0] == "  AP002  ~/.claude/settings.json:3"
    assert shown[1] == f"         OpenAI API key: {redact(fakes.OPENAI)}"  # no " masked"
    assert lines[-1].startswith(" BLOCK ")


def test_nothing_applied_says_why():
    con = _console(80)
    fixview.render_outcome(None, [], con, FailOn.high, note="Dry run: nothing changed.")
    assert _text(con) == ["", "Dry run: nothing changed.", "", " CLEAR   no prints · keys masked"]
