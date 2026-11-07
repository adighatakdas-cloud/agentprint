"""Command-line interface."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
from rich.console import Console
from typer.core import TyperCommand, TyperGroup

from . import __version__
from .advice import Moment
from .checks.ap006_package_leak import check_listing, is_agent_file
from .integrations import ALL_INTEGRATIONS
from .models import Finding
from .pack import PackError, collect
from .report import FailOn, _short, exit_code, print_table, to_agent, to_json
from .sarif import to_sarif
from .scanner import scan as run_scan
from .ui.detect import is_agent, is_interactive, plain_help, should_animate
from .ui.theme import console as themed_console

AGENT_NAMES = ", ".join(ALL_INTEGRATIONS)  # help text stays in step with the integrations


def _plain(command: TyperGroup | TyperCommand) -> None:
    """Typer's documented switch for plain help: rich_markup_mode=None (click's formatter, no
    escape codes), here set for one run on the command and every subcommand."""
    command.rich_markup_mode = None
    for sub in getattr(command, "commands", {}).values():
        _plain(sub)


class _Group(TyperGroup):
    """AI agents and pipes get plain help and usage errors. Typer's own rich console forces
    color whenever GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set, even into a pipe, so the
    choice is made here, once per run, before any help is printed (--help is handled while the
    arguments are read). A person at a terminal keeps typer's colored help."""

    def main(self, *args: Any, **kwargs: Any) -> Any:
        if plain_help(os.environ, sys.stdout):
            _plain(self)
        return super().main(*args, **kwargs)


def _print_help(ctx: typer.Context, plain: bool = False) -> None:
    """Print the help once. Typer's rich help prints itself and get_help() returns ""; plain
    help is returned as text."""
    if plain:
        _plain(ctx.command)
    text = ctx.get_help()
    if text:
        typer.echo(text)


app = typer.Typer(
    cls=_Group,
    add_completion=False,
    help="Audit what AI coding agents leave behind: keys in history files, and agent "
    "files leaking into npm/PyPI packages. Offline. Secrets are always redacted.",
)


class Format(StrEnum):
    table = "table"
    json = "json"
    sarif = "sarif"
    agent = "agent"  # compact text for an AI coding agent: verdict first, fix steps, ask_human


FormatOpt = Annotated[
    Format | None,
    typer.Option("--format", "-f", help="Output format. Default: agent inside an AI agent."),
]
PlainOpt = Annotated[bool, typer.Option("--plain", help="No color or animation.")]
FailOnOpt = Annotated[
    FailOn, typer.Option(help="Exit 1 if any finding is at or above this severity.")
]


def _version(value: bool) -> None:
    if value:
        typer.echo(f"agentprint {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version and exit."),
    ] = False,
    plain: PlainOpt = False,
) -> None:
    """agentprint."""
    # `agentprint --plain scan` works like `agentprint scan --plain`: the commands read it here.
    ctx.ensure_object(dict)["plain"] = plain
    if plain:
        _plain(ctx.command)  # and the commands' help is plain too
    if ctx.invoked_subcommand is not None:
        return
    if not is_interactive(os.environ, sys.stdin, sys.stdout):
        # Pipes, CI and AI agents: the help text, and never a wait for a key.
        _print_help(ctx, plain)
        raise typer.Exit(0)
    raise typer.Exit(_bare(ctx, plain))


def _top_plain(ctx: typer.Context, plain: bool) -> bool:
    """--plain given to this command or before it (`agentprint --plain scan`)."""
    return plain or bool(isinstance(ctx.obj, dict) and ctx.obj.get("plain"))


def _bare(ctx: typer.Context, plain: bool) -> int:
    """Bare `agentprint` for a person at a terminal: the welcome once, then the menu."""
    from .roster import roster
    from .ui import menu
    from .ui.theme import is_light
    from .ui.welcome import seen_welcome

    home, env = Path.home(), dict(os.environ)
    try:
        if not seen_welcome(home):
            _show_welcome(home, env, plain)
            return 0
        # The menu draws itself in place, so even --plain needs a terminal console (no color).
        out = themed_console(env, **({"no_color": True} if plain else {}))
        keys, numbered = menu.key_reader(sys.stdin)
        with keys as read_key:
            action = menu.choose(
                out,
                read_key,
                menu.idle_seconds(env),
                roster(home, env),
                _short(os.getcwd()),
                is_light(env),
                numbered=numbered,
            )
        return _run_action(ctx, action, plain)
    except KeyboardInterrupt:
        return 130
    except typer.Abort:  # Ctrl-C (or Ctrl-D) at a y/N question
        typer.echo("", err=True)
        return 130


def _run_action(ctx: typer.Context, action: str | None, plain: bool) -> int:
    """Run what was picked in the menu, through the same code as the command itself."""
    if action == "scan":
        return run_scan_command(Path("."), None, False, None, FailOn.high, plain)
    if action == "pack":
        if not _python_build_ok(Path("."), plain):
            return 0
        return run_pack_command(Path("."), None, FailOn.high, plain)
    if action in ("install", "uninstall"):
        return _arm(action == "install")
    if action == "status":
        return run_status_command(None, None, plain)
    if action == "help":
        _print_help(ctx, plain)
    return 0


def _python_build_ok(folder: Path, plain: bool) -> bool:
    """Menu preflight: a Python project has to be built first, and building runs its code.
    Say so and ask (default No). Other folders need no question."""
    if not ((folder / "pyproject.toml").is_file() or (folder / "setup.py").is_file()):
        return True
    out = themed_console(dict(os.environ), plain=plain)
    out.print(
        "This folder is a Python project. To preflight it, agentprint runs `uv build`, which "
        "runs the project's own build code and may download its build tools. Only do this "
        "for a project you trust.",
        markup=False,
    )
    out.print()
    if typer.confirm("Build and preflight it?", default=False):
        return True
    out.print(
        "Not built. To check a package without building it: agentprint pack <file.whl>",
        markup=False,
    )
    return False


def _arm(arm: bool) -> int:
    """Menu install/uninstall: show what would change, ask, and only change it on a yes."""
    from .install import install as run_install
    from .install import uninstall as run_uninstall

    run = run_install if arm else run_uninstall
    home_dir, env, names = _setup_args(None, None)  # your home and env, as the commands use
    if not run(home_dir, env, names, True, typer.echo, apply_hint=None):  # the menu asks
        return 0  # nothing would change, so nothing to ask
    typer.echo("")
    if typer.confirm("Arm these agents?" if arm else "Disarm these agents?", default=False):
        run(home_dir, env, names, False, typer.echo)
    return 0


F = TypeVar("F", bound=StrEnum)


def _auto_format(fmt: F | None, agent: F, human: F) -> F:
    """No --format given: an AI agent (found by its env marker) gets `agent`, a person `human`."""
    if fmt is not None:
        return fmt
    return agent if is_agent(os.environ) else human


@contextmanager
def _working(out: Console | None, plain: bool, words: str) -> Iterator[None]:
    """A dim spinner while the work runs: only for a person at a terminal, never for an AI."""
    if out is not None and should_animate(os.environ, sys.stdout, plain):
        with out.status(words, spinner="dots", spinner_style="ap.dim"):
            yield
    else:
        yield


def _emit(
    findings: list[Finding],
    fmt: Format,
    fail_on: FailOn,
    moment: Moment,
    package: Path | None = None,
    labels: Collection[str] = (),
    out: Console | None = None,
    header: str | None = None,
) -> None:
    if fmt is Format.json:
        typer.echo(to_json(findings, moment))
    elif fmt is Format.sarif:
        typer.echo(to_sarif(findings, package=package, labels=labels))
    elif fmt is Format.agent:
        typer.echo(to_agent(findings, fail_on, moment))
    else:
        out = out or themed_console(dict(os.environ))
        print_table(findings, out, fail_on=fail_on, header=header)


def _roster_size(home: Path | None) -> int:
    """How many AI agents are on the machine being swept."""
    from .roster import roster

    if home is None:
        return sum(s.found for s in roster(Path.home(), dict(os.environ)))
    return sum(s.found for s in roster(home.absolute(), {}))


def run_scan_command(
    path: Path,
    home: Path | None,
    no_home: bool,
    fmt: Format | None,
    fail_on: FailOn,
    plain: bool,
) -> int:
    """`agentprint scan`, returning the exit code (the menu runs it too)."""
    fmt = _auto_format(fmt, Format.agent, Format.table)
    home_dir = None if no_home else (home or Path.home())
    for target in (path, home_dir):
        if target is not None and not target.is_dir():
            typer.secho(f"error: {target} does not exist or is not a directory", err=True, fg="red")
            return 2
    out = themed_console(dict(os.environ), plain=plain) if fmt is Format.table else None
    started = time.perf_counter()
    with _working(out, plain, "sweeping…"):
        findings = run_scan(home_dir, path)
    header = None
    if out is not None:
        secs = time.perf_counter() - started
        if home_dir is None:
            header = f"agentprint scan  {path} · {secs:.2f}s"
        else:
            where = f"{'~' if home is None else _short(str(home))} + {path}"
            header = f"agentprint scan  {where} · roster {_roster_size(home)} · {secs:.2f}s"
    _emit(findings, fmt, fail_on, Moment.scan, out=out, header=header)
    return exit_code(findings, fail_on)


@app.command()
def scan(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Project directory to scan.")] = Path("."),
    home: Annotated[
        Path | None, typer.Option(help="Home directory to scan. Default: yours.")
    ] = None,
    no_home: Annotated[
        bool, typer.Option("--no-home", help="Only scan PATH, not your home directory.")
    ] = False,
    fmt: FormatOpt = None,
    fail_on: FailOnOpt = FailOn.high,
    plain: PlainOpt = False,
) -> None:
    """Find secrets that AI coding agents left in history and settings files."""
    plain = _top_plain(ctx, plain)
    raise typer.Exit(run_scan_command(path, home, no_home, fmt, fail_on, plain))


def run_pack_command(target: Path, fmt: Format | None, fail_on: FailOn, plain: bool) -> int:
    """`agentprint pack`, returning the exit code (the menu runs it too)."""
    fmt = _auto_format(fmt, Format.agent, Format.table)
    out = themed_console(dict(os.environ), plain=plain) if fmt is Format.table else None
    started = time.perf_counter()
    try:
        with _working(out, plain, "preflighting…"):
            listings = collect(target, is_agent_file)
    except PackError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        return 2
    secs = time.perf_counter() - started
    findings = [finding for listing in listings for finding in check_listing(listing)]
    labels = [listing.label for listing in listings]
    header = f"agentprint pack  {', '.join(labels)} · {secs:.2f}s" if out is not None else None
    _emit(
        findings,
        fmt,
        fail_on,
        Moment.publish,
        package=target,
        labels=labels,
        out=out,
        header=header,
    )
    return exit_code(findings, fail_on)


@app.command()
def pack(
    ctx: typer.Context,
    target: Annotated[
        Path,
        typer.Argument(help="Project directory, or a built package (.tgz, .tar.gz, .whl, .zip)."),
    ] = Path("."),
    fmt: FormatOpt = None,
    fail_on: FailOnOpt = FailOn.high,
    plain: PlainOpt = False,
) -> None:
    """Check what a package would publish: agent files and the keys inside them.

    npm projects are listed with `npm pack --dry-run --ignore-scripts` (no package code runs).
    Python projects are built with `uv build`, which runs the project's build backend and may
    download it from PyPI: only point it at projects you trust, or pass a built archive.
    """
    raise typer.Exit(run_pack_command(target, fmt, fail_on, _top_plain(ctx, plain)))


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


def run_status_command(home: Path | None, fmt: StatusFormat | None, plain: bool) -> int:
    """`agentprint status`, returning the exit code (the menu runs it too)."""
    import json as json_lib

    from rich.text import Text

    from .roster import roster, summary_line
    from .sanitize import clean
    from .ui.layout import hang

    if home is None:
        home_dir, env = Path.home(), dict(os.environ)
    elif home.is_dir():
        home_dir, env = home.absolute(), {}
    else:
        typer.secho(f"error: {home} is not a directory", err=True, fg=typer.colors.RED)
        return 2
    fmt = _auto_format(fmt, StatusFormat.agent, StatusFormat.table)
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
        out = themed_console(env or dict(os.environ), plain=plain)
        out.print(Text(f"agentprint {__version__} · roster", style="ap.dim"))
        for line in hang("", summary_line(states), out.width):
            out.print(line)
        width = max([14, *(len(clean(s.label)) + 2 for s in states if s.found)])
        for s in states:
            if s.found:
                line = Text(clean(s.label).ljust(width))
                line.append(*(("armed", "ap.amber") if s.armed else ("unarmed", "ap.dim")))
                out.print(line)
        missing = [clean(s.label) for s in states if not s.found]
        if missing:
            body = Text(", ".join(missing), style="ap.dim")
            for line in hang(Text("not found: ", style="ap.dim"), body, out.width):
                out.print(line)
    return 0


@app.command()
def status(
    ctx: typer.Context,
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
    raise typer.Exit(run_status_command(home, fmt, _top_plain(ctx, plain)))


@app.command()
def welcome(
    ctx: typer.Context,
    home: Annotated[
        Path | None, typer.Option(help="Home directory to check. Default: yours.")
    ] = None,
    plain: PlainOpt = False,
) -> None:
    """Show the welcome screen again."""
    if home is None:
        home_dir, env = Path.home(), dict(os.environ)
    elif home.is_dir():
        home_dir, env = home.absolute(), {}
    else:
        typer.secho(f"error: {home} is not a directory", err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    try:
        _show_welcome(home_dir, env, _top_plain(ctx, plain))
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


def _show_welcome(home_dir: Path, env: dict[str, str], plain: bool) -> None:
    """Play the welcome, then remember a person saw it (an AI agent is not a person seeing
    it). Ctrl-C raises KeyboardInterrupt."""
    from .roster import roster
    from .ui import sweep
    from .ui.theme import is_light
    from .ui.welcome import mark_welcome, welcome_lines

    theme_env = env or dict(os.environ)
    out = themed_console(theme_env, plain=plain)
    lines = welcome_lines(
        roster(home_dir, env),
        __version__,
        _short(os.getcwd()),
        light=is_light(theme_env),
        width=out.width,
    )
    sweep.play(out, lines, should_animate(theme_env, sys.stdout, plain))
    if not is_agent(os.environ):
        mark_welcome(home_dir)
