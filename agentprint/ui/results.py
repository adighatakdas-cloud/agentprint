"""Scan and pack results for a person: layout A, "Grouped".

One group per severity, two lines per print (id and where, then what), the fix list, then the
verdict. AI agents never see this layout: they get `--format agent`. Every scanned string is
cleaned and goes into rich Text as plain text, so markup in a file name prints literally.

Prose wraps at word boundaries under its own indent (ui/layout.py). Lines naming a path (the
header and each "where") are printed whole, so a long path can still be copied in one piece.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from rich.console import Console
from rich.text import Text

from agentprint.models import Finding, Severity
from agentprint.report import FailOn, _plural, _short, exit_code, summary
from agentprint.sanitize import clean, safe_step
from agentprint.ui.layout import hang
from agentprint.ui.theme import SEVERITY_STYLE

# What redact() in agentprint/secrets.py prints for a key: its first 4 characters and length.
STUB = re.compile(r"\S{1,4}…\(len \d+\)")
DETAIL_INDENT = " " * 9  # lines up with the path after "  AP001  "


def where_line(f: Finding) -> Text:
    line = Text("  ")
    line.append(f.check_id, style="ap.dim")
    line.append("  ")
    line.append(clean(_short(f.location)) + (f":{f.line}" if f.line else ""), style="ap.strong")
    return line


def _detail(f: Finding) -> Text:
    detail = clean(f.detail)
    line = Text()
    end = 0
    for stub in STUB.finditer(detail):
        line.append(detail[end : stub.start()])
        line.append(stub.group(), style="ap.amber")
        line.append(" masked", style="ap.dim")
        end = stub.end()
    line.append(detail[end:])
    if f.count > 1:
        line.append(f" (×{f.count})", style="ap.dim")
    return line


def verdict_lines(findings: list[Finding], fail_on: FailOn, width: int | None) -> list[Text]:
    prints = _plural(len(findings), "print")
    if exit_code(findings, fail_on):
        counts = " · ".join(f"{n} {name}" for name, n in summary(findings).items() if n)
        label, text = (" BLOCK ", "ap.block"), f"{prints} · {counts} · keys masked"
    elif not findings:
        label, text = (" CLEAR ", "ap.clear_label"), "no prints · keys masked"
    elif fail_on is FailOn.none:
        label = (" CLEAR ", "ap.clear_label")
        text = f"{prints} · --fail-on none (report only) · keys masked"
    else:
        label = (" CLEAR ", "ap.clear_label")
        text = f"{prints} · none at or above --fail-on {fail_on} · keys masked"
    return hang(Text.assemble(label, "  "), text, width)


def render(
    findings: list[Finding],
    console: Console,
    *,
    fail_on: FailOn = FailOn.high,
    header: str | None = None,
    not_checked: Sequence[str] = (),
) -> None:
    width = console.width
    if header is not None:
        console.print(Text(clean(header), style="ap.dim"), soft_wrap=True)  # names paths
        console.print()
    shown: list[Finding] = []
    for severity in sorted(Severity, reverse=True):
        group = [f for f in findings if f.severity is severity]
        if not group:
            continue
        name = f"{severity.name.lower():<10}"
        console.print(Text.assemble((name, SEVERITY_STYLE[severity]), (str(len(group)), "ap.dim")))
        for f in group:
            console.print(where_line(f), soft_wrap=True)  # one line, so the path copies whole
            for line in hang(DETAIL_INDENT, _detail(f), width):
                console.print(line)
        console.print()
        shown += group
    if shown:
        console.print(Text("Fix", style="ap.dim"))
        fixes = list(dict.fromkeys((f.check_id, f.fix) for f in shown))  # unique, in order
        digits = len(str(len(fixes)))  # numbers right-aligned, so the ids stay in one column
        for n, (check_id, fix) in enumerate(fixes, 1):
            lead = Text.assemble((f"  {n:>{digits}}  {check_id}", "ap.dim"), "  ")
            for line in hang(lead, safe_step(fix), width):
                console.print(line)
        console.print()
    for note in not_checked:
        for line in hang(Text("not checked  ", style="ap.dim"), clean(note), width):
            console.print(line)
    for line in verdict_lines(findings, fail_on, width):
        console.print(line)
