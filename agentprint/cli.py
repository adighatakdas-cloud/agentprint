"""Command-line interface."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .checks.ap006_package_leak import check_listing, is_agent_file
from .models import Finding
from .pack import PackError, collect
from .report import FailOn, exit_code, print_table, to_json
from .scanner import scan as run_scan

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Audit what AI coding agents leave behind: keys in history files, and agent "
    "files leaking into npm/PyPI packages. Offline. Secrets are always redacted.",
)


class Format(StrEnum):
    table = "table"
    json = "json"


FormatOpt = Annotated[Format, typer.Option("--format", "-f", help="Output format.")]
FailOnOpt = Annotated[
    FailOn, typer.Option(help="Exit 1 if any finding is at or above this severity.")
]


def _version(value: bool) -> None:
    if value:
        typer.echo(f"agentprint {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """agentprint."""


def _emit(findings: list[Finding], fmt: Format) -> None:
    if fmt is Format.json:
        typer.echo(to_json(findings))
    else:
        print_table(findings, Console())


@app.command()
def scan(
    path: Annotated[Path, typer.Argument(help="Project directory to scan.")] = Path("."),
    home: Annotated[
        Path | None, typer.Option(help="Home directory to scan. Default: yours.")
    ] = None,
    no_home: Annotated[
        bool, typer.Option("--no-home", help="Only scan PATH, not your home directory.")
    ] = False,
    fmt: FormatOpt = Format.table,
    fail_on: FailOnOpt = FailOn.high,
) -> None:
    """Find secrets that AI coding agents left in history and settings files."""
    findings = run_scan(None if no_home else (home or Path.home()), path)
    _emit(findings, fmt)
    raise typer.Exit(exit_code(findings, fail_on))


@app.command()
def pack(
    target: Annotated[
        Path,
        typer.Argument(help="Project directory, or a built package (.tgz, .tar.gz, .whl, .zip)."),
    ] = Path("."),
    fmt: FormatOpt = Format.table,
    fail_on: FailOnOpt = FailOn.high,
) -> None:
    """Check what a package would publish: agent files and the keys inside them."""
    try:
        listings = collect(target, is_agent_file)
    except PackError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(2) from exc
    findings = [finding for listing in listings for finding in check_listing(listing)]
    _emit(findings, fmt)
    raise typer.Exit(exit_code(findings, fail_on))
