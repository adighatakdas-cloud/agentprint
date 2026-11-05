"""Command-line interface."""

from __future__ import annotations

import os
import sys
from collections.abc import Collection
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .advice import Moment
from .checks.ap006_package_leak import check_listing, is_agent_file
from .integrations import ALL_INTEGRATIONS
from .models import Finding
from .pack import PackError, collect
from .report import FailOn, exit_code, print_table, to_agent, to_json
from .sarif import to_sarif
from .scanner import scan as run_scan

AGENT_NAMES = ", ".join(ALL_INTEGRATIONS)  # help text stays in step with the integrations

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Audit what AI coding agents leave behind: keys in history files, and agent "
    "files leaking into npm/PyPI packages. Offline. Secrets are always redacted.",
)


class Format(StrEnum):
    table = "table"
    json = "json"
    sarif = "sarif"
    agent = "agent"  # compact text for an AI coding agent: verdict first, fix steps, ask_human


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


def _emit(
    findings: list[Finding],
    fmt: Format,
    fail_on: FailOn,
    moment: Moment,
    package: Path | None = None,
    labels: Collection[str] = (),
) -> None:
    if fmt is Format.json:
        typer.echo(to_json(findings, moment))
    elif fmt is Format.sarif:
        typer.echo(to_sarif(findings, package=package, labels=labels))
    elif fmt is Format.agent:
        typer.echo(to_agent(findings, fail_on, moment))
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
    home_dir = None if no_home else (home or Path.home())
    for target in (path, home_dir):
        if target is not None and not target.is_dir():
            typer.secho(f"error: {target} does not exist or is not a directory", err=True, fg="red")
            raise typer.Exit(2)
    findings = run_scan(home_dir, path)
    _emit(findings, fmt, fail_on, Moment.scan)
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
    """Check what a package would publish: agent files and the keys inside them.

    npm projects are listed with `npm pack --dry-run --ignore-scripts` (no package code runs).
    Python projects are built with `uv build`, which runs the project's build backend and may
    download it from PyPI: only point it at projects you trust, or pass a built archive.
    """
    try:
        listings = collect(target, is_agent_file)
    except PackError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(2) from exc
    findings = [finding for listing in listings for finding in check_listing(listing)]
    labels = [listing.label for listing in listings]
    _emit(findings, fmt, fail_on, Moment.publish, package=target, labels=labels)
    raise typer.Exit(exit_code(findings, fail_on))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def guard(
    agent: Annotated[
        str,
        typer.Option(help=f"Agent calling the hook: {AGENT_NAMES}."),
    ] = "claude-code",
    fail_on: FailOnOpt = FailOn.high,
) -> None:
    """Hook entry point for AI agents: stop a publish or git push that would leak.

    Reads the agent's hook JSON on stdin and answers in that agent's own format. Only package
    publishes and `git push` are checked; everything else passes straight through. If
    agentprint itself fails, the command is allowed. `agentprint install` sets this up.
    """
    from .hook import run_hook  # imported here so other commands stay fast to start

    reply = run_hook(agent, sys.stdin.read(), os.environ, fail_on)
    if reply.stdout:
        typer.echo(reply.stdout)
    if reply.stderr:
        typer.echo(reply.stderr, err=True)
    raise typer.Exit(reply.exit_code)


@app.command()
def mcp() -> None:
    """Run agentprint as an MCP server on stdin/stdout, for AI agents.

    Tools: check_package, check_push, scan_secrets, explain_finding. `agentprint install`
    registers it with your agents.
    """
    from .mcp_server import build_server

    build_server().run()


AgentsOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--agent",
        help=f"Only this agent (repeatable): {AGENT_NAMES} (gemini-cli also means gemini). "
        "Default: every agent found.",
    ),
]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Show the changes, write nothing.")]
SetupHomeOpt = Annotated[
    Path | None,
    typer.Option(help="Home folder whose agent config to change. Default: yours."),
]


# Names used in scan findings that differ from the integration's name.
AGENT_ALIASES = {"gemini-cli": "gemini"}


def _setup_args(
    agents: list[str] | None, home: Path | None
) -> tuple[Path, dict[str, str], list[str] | None]:
    from .integrations import ALL_INTEGRATIONS

    if agents:
        agents = [AGENT_ALIASES.get(name, name) for name in agents]
    unknown = [name for name in agents or [] if name not in ALL_INTEGRATIONS]
    if unknown:
        typer.secho(
            f"error: unknown agent {', '.join(unknown)}. Known: {', '.join(ALL_INTEGRATIONS)}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    if home is None:
        return Path.home(), dict(os.environ), agents  # honour CODEX_HOME, CLAUDE_CONFIG_DIR, ...
    if not home.is_dir():
        typer.secho(f"error: {home} is not a directory", err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    # Agents run hooks from any folder, so every path written into their config is absolute.
    return home.absolute(), {}, agents


@app.command()
def install(agent: AgentsOpt = None, dry_run: DryRunOpt = False, home: SetupHomeOpt = None) -> None:
    """Set agentprint up inside every AI coding agent on this computer.

    For each agent found it adds a hook that checks publishes and pushes (where the agent
    supports hooks), the agentprint MCP server, and a short note in the agent's global rules.
    Every change is shown as a diff and backed up first. `agentprint uninstall` undoes it.
    """
    from .install import install as run_install

    home_dir, env, names = _setup_args(agent, home)
    run_install(home_dir, env, names, dry_run, typer.echo)


@app.command()
def uninstall(
    agent: AgentsOpt = None, dry_run: DryRunOpt = False, home: SetupHomeOpt = None
) -> None:
    """Remove what `agentprint install` added, keeping any changes you made since."""
    from .install import uninstall as run_uninstall

    home_dir, env, names = _setup_args(agent, home)
    run_uninstall(home_dir, env, names, dry_run, typer.echo)


class StatusFormat(StrEnum):
    table = "table"
    json = "json"
    agent = "agent"


@app.command()
def status(
    home: Annotated[
        Path | None, typer.Option(help="Home directory to check. Default: yours.")
    ] = None,
    fmt: Annotated[
        StatusFormat | None,
        typer.Option("--format", "-f", help="Output format. Default: agent inside an AI agent."),
    ] = None,
    plain: Annotated[bool, typer.Option("--plain", help="No colors.")] = False,
) -> None:
    """Print the roster: which AI agents are here, and which have agentprint armed."""
    import json as json_lib

    from rich.text import Text

    from .roster import roster, summary_line
    from .sanitize import clean
    from .ui.detect import is_agent
    from .ui.theme import console

    if home is None:
        home_dir, env = Path.home(), dict(os.environ)
    elif home.is_dir():
        home_dir, env = home.absolute(), {}
    else:
        typer.secho(f"error: {home} is not a directory", err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    fmt = fmt or (StatusFormat.agent if is_agent(os.environ) else StatusFormat.table)
    states = roster(home_dir, env)
    if fmt is StatusFormat.json:
        agents = [
            {"name": s.name, "label": s.label, "found": s.found, "armed": s.armed} for s in states
        ]
        typer.echo(json_lib.dumps({"tool": "agentprint", "version": __version__, "agents": agents}))
    elif fmt is StatusFormat.agent:
        for s in states:
            word = ("armed" if s.armed else "unarmed") if s.found else "not found"
            typer.echo(f"{clean(s.name)} {word}")
        if any(s.found and not s.armed for s in states):
            typer.echo("Arm unarmed agents with: agentprint install")
    else:
        out = console(env or dict(os.environ), plain=plain)
        out.print(Text(f"agentprint {__version__} · roster", style="ap.dim"))
        out.print(Text(summary_line(states)))
        width = max([14, *(len(clean(s.label)) + 2 for s in states if s.found)])
        for s in states:
            if s.found:
                line = Text(clean(s.label).ljust(width))
                line.append(*(("armed", "ap.amber") if s.armed else ("unarmed", "ap.dim")))
                out.print(line)
        missing = [clean(s.label) for s in states if not s.found]
        if missing:
            out.print(Text("not found: " + ", ".join(missing), style="ap.dim"))
