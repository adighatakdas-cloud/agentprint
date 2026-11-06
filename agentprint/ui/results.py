"""Scan and pack results for a person: layout A, "Grouped".

One group per severity, two lines per print (id and where, then what), the fix list, then the
verdict. AI agents never see this layout: they get `--format agent`. Every scanned string is
cleaned and goes into rich Text as plain text, so markup in a file name prints literally.
"""

from __future__ import annotations

import re

from rich.console import Console
from rich.text import Text

from agentprint.models import Finding, Severity
from agentprint.report import FailOn, _plural, _short, exit_code, summary
from agentprint.sanitize import clean, safe_step
from agentprint.ui.theme import SEVERITY_STYLE

# What redact() in agentprint/secrets.py prints for a key: its first 4 characters and length.
STUB = re.compile(r"\S{1,4}…\(len \d+\)")
DETAIL_INDENT = " " * 9  # lines up with the path after "  AP001  "


def _where(f: Finding) -> Text:
    line = Text("  ")
    line.append(f.check_id, style="ap.dim")
    line.append("  ")
    line.append(clean(_short(f.location)) + (f":{f.line}" if f.line else ""), style="ap.strong")
    return line


def _detail(f: Finding) -> Text:
    detail = clean(f.detail)
    line = Text(DETAIL_INDENT)
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


def _verdict(findings: list[Finding], fail_on: FailOn) -> Text:
    prints = _plural(len(findings), "print")
    if exit_code(findings, fail_on):
        counts = " · ".join(f"{n} {name}" for name, n in summary(findings).items() if n)
        return Text.assemble((" BLOCK ", "ap.block"), f"  {prints} · {counts} · keys masked")
    line = Text.assemble((" CLEAR ", "ap.clear_label"))
    if not findings:
        line.append("  no prints · keys masked")
    elif fail_on is FailOn.none:
        line.append(f"  {prints} · --fail-on none (report only) · keys masked")
    else:
        line.append(f"  {prints} · none at or above --fail-on {fail_on} · keys masked")
    return line


def render(
    findings: list[Finding],
    console: Console,
    *,
    fail_on: FailOn = FailOn.high,
    header: str | None = None,
) -> None:
    if header is not None:
        console.print(Text(clean(header), style="ap.dim"))
        console.print()
    shown: list[Finding] = []
    for severity in sorted(Severity, reverse=True):
        group = [f for f in findings if f.severity is severity]
        if not group:
            continue
        name = f"{severity.name.lower():<10}"
        console.print(Text.assemble((name, SEVERITY_STYLE[severity]), (str(len(group)), "ap.dim")))
        for f in group:
            console.print(_where(f))
            console.print(_detail(f))
        console.print()
        shown += group
    if shown:
        console.print(Text("Fix", style="ap.dim"))
        fixes = dict.fromkeys((f.check_id, f.fix) for f in shown)  # unique, in the order shown
        for n, (check_id, fix) in enumerate(fixes, 1):
            console.print(Text.assemble((f"  {n}  {check_id}", "ap.dim"), f"  {safe_step(fix)}"))
        console.print()
    console.print(_verdict(findings, fail_on))
