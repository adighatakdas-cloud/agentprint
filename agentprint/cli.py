"""Command-line interface."""

from __future__ import annotations

from typing import Annotated

import typer

from . import __version__

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Audit what AI coding agents leave behind: keys in history files, and agent "
    "files leaking into npm/PyPI packages. Offline. Secrets are always redacted.",
)


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
