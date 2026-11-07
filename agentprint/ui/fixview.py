"""`agentprint fix` for a person: the plan, then what changed and the verdict of the re-check.

Layout A conventions (ui/results.py): section names dim at column 0, items indented two
columns, paths strong and printed whole (soft_wrap) so a long path still copies in one piece,
masked key stubs amber, prose wrapped under its own column (ui/layout.py).

A fix list row is a verb (mask, env, ignore, lock), the place, and what changes. When every row
fits the terminal they are three aligned columns on one line each; otherwise the place gets a
line of its own and what changes goes under it, the way the results show a print. AI agents
never see this layout: they get fixreport.to_agent_fix.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.text import Text

from agentprint.fix import Outcome, Plan
from agentprint.fixreport import UNDOABLE, Row, fixes, rows, server_name, skipped_rows
from agentprint.models import Finding
from agentprint.report import FailOn, _short, exit_code
from agentprint.sanitize import clean
from agentprint.ui.layout import hang
from agentprint.ui.results import DETAIL_INDENT, STUB, verdict_lines, where_line

VERBS = ("mask", "env", "ignore", "lock")
PLACE = " " * (2 + max(len(v) for v in VERBS) + 2)  # where the place starts: under it, the rest
GAP = 3  # between the place and what changes, when a row is one line


def _place(path: Path, line: int | None) -> Text:
    return Text(clean(_short(str(path))) + (f":{line}" if line else ""), style="ap.strong")


def _what(row: Row) -> Text:
    """What a row changes, in a few words; a masked key's stub in amber."""
    first = row.changes[0]
    if row.kind == "mask":
        text = Text(clean(first.shown), style="ap.amber")
        places = sum(len(c.offsets) for c in row.changes)
        if places > 1:
            text.append(f" (×{places})", style="ap.dim")
        return text
    if row.kind == "env":
        return Text(f"{clean(server_name(first), 60)}.env.{first.var} → ${{{first.var}}}")
    if row.kind == "ignore":
        text = Text(f"+ {clean(first.line_text)}")
        if not os.path.lexists(first.path):
            seeded = first.seed is not None and os.path.isfile(first.seed)
            text.append(
                " (new file, copied from .gitignore)" if seeded else " (new file)", "ap.dim"
            )
        return text
    if row.kind == "lock" and first.old_mode is None:  # Windows: an access list, not a mode
        many = f"{len(row.changes)} files: " if len(row.changes) > 1 else ""
        return Text(f"{many}access list → other users removed")
    if row.kind == "lock" and len(row.changes) > 1:
        return Text(f"{len(row.changes)} files → owner only")
    if row.kind == "lock" and first.old_mode is not None:
        return Text(f"{first.old_mode:04o} → {first.old_mode & ~0o077:04o}, owner only")
    return Text(clean(first.shown))


def _slashes(rest: str) -> str:
    """The part of a path after ~, with forward slashes on Windows (doubled ones are JSON's)."""
    return rest.replace("\\\\", "/").replace("\\", "/") if os.sep == "\\" else rest


def _prose(text: str) -> Text:
    """A "needs you" sentence: quoted paths under the home folder shown from ~, masked key
    stubs in amber (as the results show them). Shell commands are never touched: agentprint
    quotes those with single quotes, display paths with double quotes."""
    home = str(Path.home())
    text = clean(text, None)
    if home not in ("", "/"):
        # Display paths are JSON-quoted, so on Windows a backslash arrives doubled: match both.
        homes = "|".join(re.escape(v) for v in {home, home.replace("\\", "\\\\")})
        text = re.sub(
            f'"(?:{homes})' + r'(?=[/\\"])([/\\][^"]*)?',
            lambda m: '"~' + _slashes(m.group(1) or ""),
            text,
        )
    out = Text()
    end = 0
    for stub in STUB.finditer(text):
        out.append(text[end : stub.start()])
        out.append(stub.group(), style="ap.amber")
        end = stub.end()
    out.append(text[end:])
    return out


def _print_list(console: Console, items: list[tuple[str, Text, Text]]) -> None:
    """Rows of (verb, place, what changes), aligned in columns."""
    width = console.width
    room = max(place.cell_len for _, place, _ in items)
    one_line = all(len(PLACE) + room + GAP + what.cell_len <= width for _, _, what in items)
    for verb, place, what in items:
        lead = Text(f"  {verb:<{len(PLACE) - 4}}  ")
        if one_line:
            gap = " " * (room - place.cell_len + GAP)
            console.print(Text.assemble(lead, place, gap, what), soft_wrap=True)
        else:
            console.print(Text.assemble(lead, place), soft_wrap=True)  # the path copies whole
            for line in hang(PLACE, what, width):
                console.print(line)


def _numbered(console: Console, items: list[tuple[str, str]]) -> None:
    """Numbered sentences (check id, text), wrapped under their own column (as results' Fix)."""
    digits = len(str(len(items)))
    for n, (check_id, text) in enumerate(items, 1):
        lead = Text.assemble((f"  {n:>{digits}}  {check_id}", "ap.dim"), "  ")
        for line in hang(lead, _prose(text), console.width):
            console.print(line)


def _dim(console: Console, text: str, lead: str = "") -> None:
    for line in hang(lead, Text(text, style="ap.dim"), console.width):
        console.print(line)


class _Blocks:
    """Blank lines between blocks, never before the first one or after the last one."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.started = False

    def next(self) -> Console:
        if self.started:
            self.console.print()
        self.started = True
        return self.console


def keep_note(planned: list[Row]) -> list[str]:
    """The plan's plain words about undo, one sentence a line: a key that was masked or replaced
    is gone for good."""
    kinds = {row.kind for row in planned}
    words = []
    if kinds & {"mask", "env"}:
        words.append(
            "A key is gone for good once masked or replaced: agentprint keeps no copy, so mask "
            "and env can't be undone."
        )
    if kinds & set(UNDOABLE):
        words.append("agentprint fix --undo puts back ignore and lock.")
    return words


def render_plan(plan: Plan, console: Console, *, header: str | None = None) -> None:
    """The header (`header` plus how many fixes are ready), what agentprint will change, what
    only the human can do, notes that need nothing done, and what can't be undone. Ends
    without a blank line, so the y/N question follows the plan directly."""
    planned = rows(plan.changes, plan.findings)
    todo, notes = plan.todo, plan.notes
    blocks = _Blocks(console)
    if header is not None:
        if planned:
            count = f"{fixes(len(planned))} ready"
        else:
            count = "nothing to fix automatically" if todo else "nothing to fix"
        title = Text(clean(f"{header} · {count}"), style="ap.dim")
        blocks.next().print(title, soft_wrap=True)  # names paths: printed whole
    if planned:
        blocks.next().print(Text("Fixing", style="ap.dim"))
        _print_list(console, [(r.kind, _place(r.path, r.line), _what(r)) for r in planned])
    if todo:
        blocks.next().print(Text("Needs you", style="ap.dim"))
        # Each item as worded for a person (the agent view keeps the agent wording).
        _numbered(console, [(m.check_id, m.person or m.text) for m in todo])
    if notes:
        blocks.next().print(Text("Notes · nothing needs doing", style="ap.dim"))
        _numbered(console, [(m.check_id, m.person or m.text) for m in notes])
    if planned:
        blocks.next()
        for sentence in keep_note(planned):
            _dim(console, sentence)


def render_outcome(
    outcome: Outcome | None,
    after_findings: list[Finding],
    console: Console,
    fail_on: FailOn,
    *,
    plan: Plan | None = None,
    note: str | None = None,
    not_checked: Sequence[str] = (),
) -> None:
    """After the plan (and the y/N question): what was fixed and what was not, what still
    blocks, then the verdict of the re-check (BLOCK or CLEAR, from ui.results). `outcome` None:
    nothing was applied, and `note` says why. Starts with a blank line."""
    width = console.width
    console.print()
    blocks = _Blocks(console)
    if outcome is None:
        if note:
            _dim(blocks.next(), note)
    else:
        findings = plan.findings if plan else []
        done = rows(outcome.applied, findings)
        skipped = skipped_rows(outcome, findings)
        parts = [f"{len(done)} fixed"]
        if skipped:
            parts.append(f"{len(skipped)} not fixed")
        if any(c.kind in UNDOABLE for c in outcome.applied):
            parts.append("undo ignore and lock with agentprint fix --undo")
        _dim(blocks.next(), " · ".join(parts))
        if skipped:
            blocks.next().print(Text("Not fixed", style="ap.dim"))
            _print_list(
                console, [(r.kind, _place(r.path, r.line), Text(clean(why))) for r, why in skipped]
            )
        blocking = [f for f in after_findings if exit_code([f], fail_on)]
        if blocking:
            blocks.next().print(Text("Still blocking", style="ap.dim"))
            for f in blocking:
                console.print(where_line(f), soft_wrap=True)  # the path copies whole
                # Not results' detail: its " masked" (the key is shown masked) would read here as
                # "fix masked it", and this key is still in its file.
                for line in hang(DETAIL_INDENT, _prose(f.detail), width):
                    console.print(line)
    for gap in not_checked:
        blocks.next()
        for line in hang(Text("not checked  ", style="ap.dim"), clean(gap), width):
            console.print(line)
    blocks.next()
    for line in verdict_lines(after_findings, fail_on, width):
        console.print(line)


def _bullets(console: Console, texts: list[str]) -> None:
    lead = Text.assemble("  ", ("·", "ap.dim"), " ")
    for text in texts:
        for line in hang(lead, _prose(text), console.width):
            console.print(line)


def render_undo(restored: list[str], cannot: list[str], nothing: bool, console: Console) -> None:
    """`agentprint fix --undo` for a person: what was put back, and what can't be."""
    if nothing:
        for text in cannot:
            for line in hang("", _prose(text), console.width):
                console.print(line)
        return
    if not restored and not cannot:
        console.print(Text("The last fix run has nothing left to undo.", style="ap.dim"))
        return
    blocks = _Blocks(console)
    for title, texts in (("Put back", restored), ("Can't undo", cannot)):
        if texts:
            blocks.next().print(Text(title, style="ap.dim"))
            _bullets(console, texts)
