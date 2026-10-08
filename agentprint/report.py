"""Render findings and turn them into an exit code."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .models import Finding, Severity

STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
}


class FailOn(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"
    none = "none"


def summary(findings: list[Finding]) -> dict[str, int]:
    return {
        s.name.lower(): sum(1 for f in findings if f.severity is s)
        for s in sorted(Severity, reverse=True)
    }


def to_json(findings: list[Finding]) -> str:
    return json.dumps(
        {
            "tool": "agentprint",
            "version": __version__,
            "summary": summary(findings),
            "findings": [f.to_dict() for f in findings],
        },
        indent=2,
    )


def _short(location: str) -> str:
    home = str(Path.home())
    return "~" + location[len(home) :] if location.startswith(home) else location


def print_table(findings: list[Finding], console: Console) -> None:
    if not findings:
        console.print("[green]No findings.[/green] Nothing risky in the places agentprint checks.")
        return
    table = Table()
    for column in ("Severity", "ID", "Agent", "Where", "What"):
        table.add_column(column)
    for f in findings:
        where = _short(f.location) + (f":{f.line}" if f.line else "")
        what = f.detail + (f" (×{f.count})" if f.count > 1 else "")
        severity = f.severity.name.lower()
        table.add_row(
            f"[{STYLE[f.severity]}]{severity}[/]", f.check_id, f.agent, escape(where), escape(what)
        )
    console.print(table)
    console.print("[bold]How to fix[/bold]")
    for check_id, fix in sorted({(f.check_id, f.fix) for f in findings}):
        console.print(f"  {check_id}: {escape(fix)}")
    counts = ", ".join(f"{n} {s}" for s, n in summary(findings).items() if n)
    console.print(f"{len(findings)} findings ({counts}). Secrets are shown redacted.")


def exit_code(findings: list[Finding], fail_on: FailOn) -> int:
    if fail_on is FailOn.none:
        return 0
    threshold = Severity[fail_on.name.upper()]
    return 1 if any(f.severity >= threshold for f in findings) else 0
