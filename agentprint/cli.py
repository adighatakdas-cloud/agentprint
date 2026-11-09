"""Command-line interface."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypeVar, cast

import typer
from rich.console import Console
from typer.core import TyperCommand, TyperGroup

from . import __version__, platform
from .advice import Moment
from .checks import PACKAGE_WANT, check_package
from .config import Sources
from .integrations import ALL_INTEGRATIONS
from .models import Finding
from .pack import PackError, collect
from .report import FailOn, _short, exit_code, print_table, to_agent, to_json
from .sarif import to_sarif
from .scanner import scan as run_scan
from .ui.detect import is_agent, is_interactive, plain_help, should_animate
from .ui.theme import console as themed_console

if TYPE_CHECKING:
    from .baseline import Known, Project, Settled
    from .roster import AgentState

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
        _plain(cast("TyperGroup | TyperCommand", ctx.command))  # click's type; typer's at runtime
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
BadgeOpt = Annotated[
    Path | None,
    typer.Option(
        "--badge",
        help="Also write badge.json and verdict.json (CLEAR or BLOCK, counts only) to this folder.",
    ),
]
FailOnOpt = Annotated[
    FailOn, typer.Option(help="Exit 1 if any finding is at or above this severity.")
]
ConfigOpt = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help="Project config to read. Default: .agentprint.toml in the project, else "
        "[tool.agentprint] in its pyproject.toml.",
    ),
]
NoConfigOpt = Annotated[
    bool,
    typer.Option("--no-config", help="Read no project config and no baseline: every print counts."),
]
BaselineOpt = Annotated[
    Path | None,
    typer.Option(
        "--baseline",
        help="Baseline of accepted findings. Default: .agentprint-baseline.json in the project.",
    ),
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
        _plain(
            cast("TyperGroup | TyperCommand", ctx.command)
        )  # and the commands' help is plain too
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
        console_options: dict[str, Any] = {"no_color": True} if plain else {}
        out = themed_console(env, **console_options)
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
        return run_scan_command(Path("."), None, False, None, None, plain)
    if action == "pack":
        if not _python_build_ok(Path("."), plain):
            return 0
        return run_pack_command(Path("."), None, None, plain)
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
    not_checked: Sequence[str] = (),
    known: Sequence[Known] = (),
) -> None:
    if fmt is Format.json:
        typer.echo(to_json(findings, moment, not_checked, known))
    elif fmt is Format.sarif:  # only what counts: known findings are left out
        typer.echo(to_sarif(findings, package=package, labels=labels))
    elif fmt is Format.agent:
        typer.echo(to_agent(findings, fail_on, moment, not_checked=not_checked, known=known))
    else:
        out = out or themed_console(dict(os.environ))
        print_table(
            findings, out, fail_on=fail_on, header=header, not_checked=not_checked, known=known
        )


def _roster_size(home: Path | None) -> int:
    """How many AI agents are on the machine being swept."""
    from .roster import roster

    if home is None:
        return sum(s.found for s in roster(Path.home(), dict(os.environ)))
    return sum(s.found for s in roster(home.absolute(), {}))


def _write_badge(
    directory: Path | None, command: str, fail_on: FailOn, findings: list[Finding], known: int = 0
) -> int:
    """After the normal output: write the badge files. 0, or 2 with a message if it can't."""
    if directory is None:
        return 0
    from . import badge

    try:
        badge.write(directory, command=command, fail_on=fail_on, findings=findings, known=known)
    except OSError as exc:
        why = exc.strerror or type(exc).__name__
        typer.secho(f"error: cannot write the badge to {directory}: {why}", err=True, fg="red")
        return 2
    return 0


def _given(ctx: typer.Context, value: FailOn, name: str = "fail_on") -> FailOn | None:
    """An option's value if it was typed on the command line; None lets the config decide.
    (By the source's name: typer ships its own copy of click's ParameterSource.)"""
    source = ctx.get_parameter_source(name)
    return value if getattr(source, "name", None) == "COMMANDLINE" else None


def _project(root: Path, sources: Sources | None, home: Path | None = None) -> Project:
    """The project's config and baseline (and with `home`, the personal baseline in it), each
    warning about them on stderr."""
    from .baseline import load

    project = load(root, sources, home)
    for warning in project.warnings:
        typer.secho(f"warning: {warning}", err=True, fg=typer.colors.YELLOW)
    return project


def run_scan_command(
    path: Path,
    home: Path | None,
    no_home: bool,
    fmt: Format | None,
    fail_on: FailOn | None,
    plain: bool,
    badge_dir: Path | None = None,
    sources: Sources | None = None,
) -> int:
    """`agentprint scan`, returning the exit code (the menu runs it too). `fail_on` None: the
    project config's, else high."""
    from .baseline import settle

    fmt = _auto_format(fmt, Format.agent, Format.table)
    home_dir = None if no_home else (home or Path.home())
    for target in (path, home_dir):
        if target is not None and not target.is_dir():
            typer.secho(f"error: {target} does not exist or is not a directory", err=True, fg="red")
            return 2
    project = _project(path, sources, home_dir)
    out = themed_console(dict(os.environ), plain=plain) if fmt is Format.table else None
    started = time.perf_counter()
    with _working(out, plain, "sweeping…"):
        settled = settle(
            run_scan(home_dir, path), project, fail_on, publishing=False, home=home_dir
        )
    findings, fail_on = settled.active, settled.fail_on
    header = None
    if out is not None:
        secs = time.perf_counter() - started
        if home_dir is None:
            header = f"agentprint scan  {path} · {secs:.2f}s"
        else:
            where = f"{'~' if home is None else _short(str(home))} + {path}"
            header = f"agentprint scan  {where} · roster {_roster_size(home)} · {secs:.2f}s"
    _emit(
        findings,
        fmt,
        fail_on,
        Moment.scan,
        out=out,
        header=header,
        not_checked=platform.scan_notes(),
        known=settled.known,
    )
    if fmt is Format.agent:
        from .protect import protection_line

        protected = protection_line(path)  # only in a protected repo: other output is unchanged
        if protected:
            typer.echo(protected)
    known = len(settled.known)
    return _write_badge(badge_dir, "scan", fail_on, findings, known) or exit_code(findings, fail_on)


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
    badge: BadgeOpt = None,
    config: ConfigOpt = None,
    no_config: NoConfigOpt = False,
    baseline_file: BaselineOpt = None,
) -> None:
    """Find secrets that AI coding agents left in history and settings files."""
    plain = _top_plain(ctx, plain)
    sources = Sources(config, no_config, baseline_file)
    raise typer.Exit(
        run_scan_command(
            path, home, no_home, fmt, _given(ctx, fail_on), plain, badge, sources=sources
        )
    )


class IfUnavailable(StrEnum):
    block = "block"
    allow = "allow"


def run_pack_command(
    target: Path,
    fmt: Format | None,
    fail_on: FailOn | None,
    plain: bool,
    badge_dir: Path | None = None,
    sources: Sources | None = None,
    if_unavailable: IfUnavailable = IfUnavailable.block,
) -> int:
    """`agentprint pack`, returning the exit code (the menu runs it too). The project config is
    read from the target folder (for a built file, from the current folder); `fail_on` None:
    the config's, else high. With `if_unavailable` allow, a package it cannot check exits 0
    with one line instead of 2 (what `agentprint protect` puts in a shared prepublishOnly)."""
    from .baseline import settle

    fmt = _auto_format(fmt, Format.agent, Format.table)
    project = _project(target if target.is_dir() else Path("."), sources)
    out = themed_console(dict(os.environ), plain=plain) if fmt is Format.table else None
    started = time.perf_counter()
    try:
        with _working(out, plain, "preflighting…"):
            listings = collect(target, PACKAGE_WANT)
    except PackError as exc:
        if if_unavailable is IfUnavailable.allow:
            from .guard import _masked

            typer.echo(
                f"agentprint: could not check this package ({_masked(str(exc))}); the publish "
                "goes on (--if-unavailable allow).",
                err=True,
            )
            return 0
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        return 2
    secs = time.perf_counter() - started
    findings = [finding for listing in listings for finding in check_package(listing)]
    # Ruling C1: a key in the package always counts; ignore and baseline cover the rest.
    settled = settle(findings, project, fail_on, publishing=True, package=True)
    findings, fail_on = settled.active, settled.fail_on
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
        known=settled.known,
    )
    known = len(settled.known)
    return _write_badge(badge_dir, "pack", fail_on, findings, known) or exit_code(findings, fail_on)


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
    badge: BadgeOpt = None,
    config: ConfigOpt = None,
    no_config: NoConfigOpt = False,
    baseline_file: BaselineOpt = None,
    if_unavailable: Annotated[
        IfUnavailable,
        typer.Option(
            "--if-unavailable",
            help="When the package can't be checked: block (exit 2) or allow (exit 0, one line).",
        ),
    ] = IfUnavailable.block,
) -> None:
    """Check what a package would publish: agent files, the keys inside them, and what lets an
    AI rebuild your source (source maps, original source, AI instruction files).

    npm projects are listed with `npm pack --dry-run --ignore-scripts` (no package code runs).
    Python projects are built with `uv build`, which runs the project's build backend and may
    download it from PyPI: only point it at projects you trust, or pass a built archive.
    """
    plain = _top_plain(ctx, plain)
    sources = Sources(config, no_config, baseline_file)
    given = _given(ctx, fail_on)
    code = run_pack_command(target, fmt, given, plain, badge, sources, if_unavailable)
    raise typer.Exit(code)


class FixFormat(StrEnum):
    table = "table"
    json = "json"
    agent = "agent"


def _check_ids(only: str | None) -> set[str] | None:
    """--only AP001,AP008 as a set of check ids (None: every check). Exits 2 on an unknown id."""
    if only is None:
        return None
    from .checks import PACKAGE_CHECK_IDS, SCAN_CHECKS

    known = sorted({check.id for check in SCAN_CHECKS} | set(PACKAGE_CHECK_IDS))
    wanted = {part.strip().upper() for part in only.split(",") if part.strip()}
    unknown = sorted(wanted - set(known))
    if unknown or not wanted:
        typer.secho(
            f"error: unknown check {', '.join(unknown) or repr(only)}. Known: {', '.join(known)}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    return wanted


def _run_undo(home: Path | None, fmt: FixFormat, plain: bool) -> int:
    """`agentprint fix --undo`: put back what the newest fix journal can. Always exit 0."""
    from . import fix as fixer
    from .fixreport import to_agent_undo, to_json_undo
    from .ui.fixview import render_undo

    if home is not None and not home.is_dir():
        typer.secho(f"error: {home} does not exist or is not a directory", err=True, fg="red")
        return 2
    restored, cannot = fixer.undo((home or Path.home()).absolute())
    nothing = not restored and len(cannot) == 1 and cannot[0].startswith(fixer.NOTHING_TO_UNDO)
    if fmt is FixFormat.json:
        typer.echo(to_json_undo(restored, cannot, nothing))
    elif fmt is FixFormat.agent:
        typer.echo(to_agent_undo(restored, cannot, nothing))
    else:
        render_undo(restored, cannot, nothing, themed_console(dict(os.environ), plain=plain))
    return 0


def run_fix_command(
    path: Path,
    home: Path | None,
    no_home: bool,
    pack: bool,
    only: str | None,
    dry_run: bool,
    yes: bool,
    fail_on: FailOn | None,
    fmt: FixFormat | None,
    plain: bool,
    sources: Sources | None = None,
) -> int:
    """`agentprint fix`: sweep (or preflight), plan, ask or take --yes, apply, check again.

    It asks only a person at a real terminal (never an AI agent, a pipe or CI), default No.
    Without that answer or --yes, it prints the plan and changes nothing. Findings the project
    accepted (its baseline and ignore rules) are left alone."""
    from . import fix as fixer
    from .baseline import settle
    from .fixreport import RERUN, fixes, rows, to_agent_fix, to_json_fix
    from .ui import fixview

    fmt = _auto_format(fmt, FixFormat.agent, FixFormat.table)
    wanted = _check_ids(only)
    if home is not None and not home.is_dir():  # the journal goes there: never create it
        typer.secho(f"error: {home} does not exist or is not a directory", err=True, fg="red")
        return 2
    out = themed_console(dict(os.environ), plain=plain) if fmt is FixFormat.table else None
    root: Path | None = None
    if pack:

        def check() -> list[Finding]:
            listings = collect(path, PACKAGE_WANT)
            return [finding for listing in listings for finding in check_package(listing)]

        root = path.absolute() if path.is_dir() else None
        header = f"agentprint fix --pack  {path}"
        working = "preflighting…"
    else:
        home_dir = None if no_home else (home or Path.home())
        if not path.is_dir():
            typer.secho(f"error: {path} does not exist or is not a directory", err=True, fg="red")
            return 2
        sweep_home = home_dir.absolute() if home_dir is not None else None

        def check() -> list[Finding]:
            # Absolute paths, so the journal names files that --undo can find from any folder.
            return run_scan(sweep_home, path.absolute())

        where = (
            str(path)
            if home_dir is None
            else f"{'~' if home is None else _short(str(home))} + {path}"
        )
        header = f"agentprint fix  {where}"
        working = "sweeping…"
    swept_home = None if pack or no_home else (home or Path.home()).absolute()
    project = _project(path if path.is_dir() else Path("."), sources, swept_home)

    def counted(found: list[Finding], given: FailOn | None = fail_on) -> Settled:
        return settle(found, project, given, publishing=pack, package=pack, home=swept_home)

    try:
        with _working(out, plain, working):
            settled = counted(check())
    except PackError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        return 2
    before = settled.active
    scope = [f for f in before if wanted is None or f.check_id in wanted]
    # The scan's own Finding objects: AP008 hands its file list over on them (Finding.paths).
    the_plan = fixer.plan(scope, pack=pack, root=root)
    if out is not None:
        fixview.render_plan(the_plan, out, header=header)

    count = len(rows(the_plan.changes, the_plan.findings))
    ask = (
        bool(count)
        and fmt is FixFormat.table
        and not (yes or dry_run)
        and is_interactive(os.environ, sys.stdin, sys.stdout)
        and not is_agent(os.environ)
    )
    go = bool(count) and not dry_run and yes
    if ask:
        try:
            go = typer.confirm(f"Apply {fixes(count)}?", default=False, prompt_suffix=" ")
        except typer.Abort:  # Ctrl-C or Ctrl-D at the question: nothing changed
            typer.echo("")
            return 130
    note = None
    if count and not go:
        note = "Dry run: nothing changed." if dry_run else "Nothing changed."
        if not (dry_run or ask):
            note = f"Nothing changed: {RERUN}."

    outcome, status = None, 0
    if go:
        with _working(out, plain, "fixing…"):
            outcome = fixer.apply(the_plan, (home or Path.home()).absolute())
            try:
                settled = counted(check())
            except PackError as exc:  # changes made, but the preflight can't run again
                typer.secho(f"error: could not check again: {exc}", err=True, fg="red")
                status = 2
    after, known, fail_on = settled.active, settled.known, settled.fail_on
    gaps = () if pack else platform.scan_notes()  # a package check reads no permissions
    if fmt is FixFormat.json:
        typer.echo(
            to_json_fix(the_plan, outcome, after, fail_on, pack=pack, not_checked=gaps, known=known)
        )
    elif fmt is FixFormat.agent:
        typer.echo(
            to_agent_fix(
                the_plan,
                outcome,
                after,
                fail_on,
                dry_run=dry_run,
                not_checked=gaps,
                known=known,
            )
        )
    else:
        assert out is not None
        fixview.render_outcome(
            outcome, after, out, fail_on, plan=the_plan, note=note, not_checked=gaps, known=known
        )
    return status or exit_code(after, fail_on)


@app.command()
def fix(
    ctx: typer.Context,
    path: Annotated[
        Path,
        typer.Argument(help="Project directory; with --pack, a project or a built package."),
    ] = Path("."),
    home: Annotated[
        Path | None,
        typer.Option(
            help="Home directory to sweep and fix (its journal goes there). Default: yours."
        ),
    ] = None,
    no_home: Annotated[
        bool, typer.Option("--no-home", help="Only fix PATH, not your home directory.")
    ] = False,
    pack: Annotated[
        bool,
        typer.Option(
            "--pack",
            help="Fix what `agentprint pack` finds in PATH instead (a Python project is built "
            "first, as pack does: only for projects you trust).",
        ),
    ] = False,
    only: Annotated[
        str | None, typer.Option(help="Only these checks, comma-separated, e.g. AP001,AP008.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan, change nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Apply without asking.")] = False,
    fail_on: FailOnOpt = FailOn.high,
    fmt: Annotated[
        FixFormat | None,
        typer.Option("--format", "-f", help="Output format. Default: agent inside an AI agent."),
    ] = None,
    undo: Annotated[
        bool, typer.Option("--undo", help="Put back the ignore and lock changes of the last fix.")
    ] = False,
    plain: PlainOpt = False,
    config: ConfigOpt = None,
    no_config: NoConfigOpt = False,
    baseline_file: BaselineOpt = None,
) -> None:
    """Fix the prints a sweep finds, then check again: BLOCK to CLEAR in one command.

    Masks keys in place in history files and permission lists (no copy is kept), points Claude
    Code MCP entries at an environment variable instead of an inline key, adds ignore lines and
    makes agent files private to you. It shows the plan first and changes nothing until you
    answer y, or pass --yes; an AI agent is never asked. What only you can do, like rotating a
    key, is listed under "Needs you". Exit 1 if anything at or above --fail-on remains.
    """
    plain = _top_plain(ctx, plain)
    if undo:
        if dry_run or pack or only:
            typer.secho(
                "error: --undo puts back the whole last fix run; it takes no --dry-run, --pack "
                "or --only",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(2)
        raise typer.Exit(
            _run_undo(home, _auto_format(fmt, FixFormat.agent, FixFormat.table), plain)
        )
    sources = Sources(config, no_config, baseline_file)
    given = _given(ctx, fail_on)
    raise typer.Exit(
        run_fix_command(path, home, no_home, pack, only, dry_run, yes, given, fmt, plain, sources)
    )


class BaselineFormat(StrEnum):
    table = "table"
    json = "json"
    agent = "agent"


def run_baseline_command(
    path: Path,
    mode: str,
    home: Path | None,
    sources: Sources,
    fmt: BaselineFormat | None,
    plain: bool,
) -> int:
    """`agentprint baseline`: write (or --update, --prune, --check) the project's baseline, and
    with a home folder your personal one (<home>/.agentprint/baseline.json) for its findings."""
    from . import baseline as bl

    fmt = _auto_format(fmt, BaselineFormat.agent, BaselineFormat.table)
    for target in (path, home):
        if target is not None and not target.is_dir():
            typer.secho(f"error: {target} does not exist or is not a directory", err=True, fg="red")
            return 2
    if sources.off and sources.baseline is None:
        typer.secho(
            "error: --no-config leaves no baseline file: pass --baseline", err=True, fg="red"
        )
        return 2
    project = _project(path, sources, home)
    file = project.config.baseline
    assert file is not None
    # Findings an ignore rule covers are known already; the baseline takes the rest.
    findings = run_scan(home, path)
    counted = bl.settle(findings, bl.Project(project.config, {}), None, publishing=False, home=home)
    # Each file: where it is, its entries, today's findings for it, whether it is personal, and
    # the folder it must stay in (unless --baseline named it).
    targets = [(file, project.entries, bl.tally(counted.active, path, home), False, path)]
    if home is not None:
        now = bl.tally(counted.active, path, home, personal=True)
        targets.append((home / bl.PERSONAL, project.personal, now, True, home))
    if mode == "check":
        checked = [
            bl.Checked(_short(str(f)), bl.check(old, now), personal)
            for f, old, now, personal, _ in targets
        ]
        if fmt is BaselineFormat.json:
            typer.echo(bl.to_json_check(checked))
        elif fmt is BaselineFormat.agent:
            typer.echo(bl.to_agent_check(checked))
        else:
            _print_baseline_check(checked, plain)
        return 0
    written = []
    for target, old, now, personal, folder in targets:
        named = sources.baseline is not None and not personal
        if not _baseline_writable(target, folder, named, personal):
            return 2
        change = bl.rewrite(old, now, mode, f"accepted {bl.today()}")
        if not _write_baseline(target, bl.dumps(change.entries), personal=personal):
            return 2
        written.append(bl.Written(_short(str(target)), change, personal))
    if fmt is BaselineFormat.json:
        typer.echo(bl.to_json_write(written, mode))
    elif fmt is BaselineFormat.agent:
        typer.echo(bl.to_agent_write(written, mode))
    else:
        _print_baseline_write(written, plain)
    return 0


def _write_baseline(file: Path, text: str, personal: bool) -> bool:
    """The project's baseline is shared with the team (0644); a personal one is yours (0600, in
    a 0700 folder)."""
    from .safewrite import atomic_write

    try:
        if personal and not file.parent.is_dir():
            file.parent.mkdir(mode=0o700)
        atomic_write(file, text, mode=0o600 if personal else 0o644)
    except OSError as exc:
        why = exc.strerror or type(exc).__name__
        typer.secho(f"error: cannot write the baseline to {file}: {why}", err=True, fg="red")
        return False
    return True


def _baseline_writable(file: Path, root: Path, chosen: bool, personal: bool = False) -> bool:
    """Never write through a link, and never outside the project (or, for the personal one,
    the home folder) unless --baseline said so: a cloned repository could commit the file as a
    link to ~/.bashrc."""
    folder, project = Path(os.path.realpath(file.parent)), Path(os.path.realpath(root))
    if file.is_symlink():
        why = "is a symbolic link"
    elif not chosen and not folder.is_relative_to(project):
        why = f"is outside the {'home folder' if personal else 'project'}"
    else:
        return True
    typer.secho(f"error: {file} {why}; agentprint does not write it", err=True, fg="red")
    return False


def _print_baseline_write(written: list[Any], plain: bool) -> None:
    from rich.text import Text

    from .baseline import to_human_write
    from .sanitize import clean
    from .ui.layout import hang

    out = themed_console(dict(os.environ), plain=plain)
    out.print(
        Text(f"agentprint baseline  {clean(written[0].file)}", style="ap.dim"), soft_wrap=True
    )
    out.print()
    lines = to_human_write(written)
    for counts in lines[: len(written)]:
        out.print(Text(clean(counts)), soft_wrap=True)
    out.print(Text("Fingerprints only: no key is stored.", style="ap.dim"))
    for step in lines[len(written) :]:
        for line in hang("  - ", step, out.width):
            out.print(line)


def _print_baseline_check(checked: list[Any], plain: bool) -> None:
    from rich.text import Text

    from .sanitize import clean

    out = themed_console(dict(os.environ), plain=plain)
    head = f"agentprint baseline --check  {clean(checked[0].file)}"
    out.print(Text(head, style="ap.dim"), soft_wrap=True)
    out.print()
    for c in checked:
        r = c.result
        entries = f"{r.entries} entr{'y' if r.entries == 1 else 'ies'}"
        where = f"personal {clean(c.file)}: " if c.personal else ""
        text = f"{where}{entries} · {r.matched} still found · {len(r.stale)} stale · {r.new} new"
        out.print(Text(text), soft_wrap=True)
    stale = [e for c in checked for e in c.result.stale]
    if stale:
        out.print()
        out.print(Text("stale", style="ap.dim"))
        for e in stale:
            line = Text("  ")
            line.append(e.check, style="ap.dim")
            line.append("  ")
            line.append(clean(e.path), style="ap.strong")
            line.append(f"  {clean(e.reason)}", style="ap.dim")
            out.print(line, soft_wrap=True)
    out.print()
    tips = ["Nothing changed."]
    if stale:
        tips.append("agentprint baseline --prune drops the stale entries.")
    if any(c.result.new for c in checked):
        tips.append("agentprint scan shows the new ones.")
    out.print(Text(" ".join(tips), style="ap.dim"))


@app.command("baseline")
def baseline_command(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
    update: Annotated[
        bool, typer.Option("--update", help="Add new findings, keep every old entry.")
    ] = False,
    prune: Annotated[
        bool, typer.Option("--prune", help="Drop entries whose finding is gone; add nothing.")
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Change nothing: list stale entries and count new findings."),
    ] = False,
    include_home: Annotated[
        bool,
        typer.Option(
            "--include-home",
            help="Also accept your home folder's findings (personal: keep the "
            "file out of git then).",
        ),
    ] = False,
    home: Annotated[
        Path | None, typer.Option(help="Home directory for --include-home. Default: yours.")
    ] = None,
    config: ConfigOpt = None,
    no_config: NoConfigOpt = False,
    baseline_file: BaselineOpt = None,
    fmt: Annotated[
        BaselineFormat | None,
        typer.Option("--format", "-f", help="Output format. Default: agent inside an AI agent."),
    ] = None,
    plain: PlainOpt = False,
) -> None:
    """Accept today's findings: write them to .agentprint-baseline.json, so `scan` and `fix`
    count them as known (listed apart) instead of failing on them.

    The file holds fingerprints only (a hash of the check, the file, the rule and the masked
    stub), never a key. Only the project's findings, unless --include-home. Without a flag the
    file is rewritten from today's findings (reasons of entries still found are kept). A key in
    a package being published or in a push still blocks, whatever the baseline says.
    """
    if update + prune + check > 1:
        typer.secho("error: pick one of --update, --prune and --check", err=True, fg="red")
        raise typer.Exit(2)
    mode = "update" if update else "prune" if prune else "check" if check else "write"
    home_dir = (home or Path.home()) if include_home or home is not None else None
    sources = Sources(config, no_config, baseline_file)
    plain = _top_plain(ctx, plain)
    raise typer.Exit(run_baseline_command(path, mode, home_dir, sources, fmt, plain))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def guard(
    ctx: typer.Context,
    agent: Annotated[
        str,
        typer.Option(help=f"Agent calling the hook: {AGENT_NAMES}."),
    ] = "claude-code",
    fail_on: FailOnOpt = FailOn.high,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Block (instead of allowing with a warning) when agentprint cannot check a "
            "publish or push. Same as AGENTPRINT_STRICT=1.",
        ),
    ] = False,
) -> None:
    """Hook entry point for AI agents: stop a publish or git push that would leak.

    Reads the agent's hook JSON on stdin and answers in that agent's own format. Only package
    publishes and `git push` are checked; everything else passes straight through. If
    agentprint itself fails, the command is allowed (with --strict it is blocked).
    `agentprint install` sets this up.
    """
    from .hook import run_hook  # imported here so other commands stay fast to start

    platform.use_utf8_streams()  # a Windows console is cp1252 unless told otherwise
    reply = run_hook(agent, sys.stdin.read(), os.environ, _given(ctx, fail_on), strict)
    if reply.stdout:
        typer.echo(reply.stdout)
    if reply.stderr:
        typer.echo(reply.stderr, err=True)
    raise typer.Exit(reply.exit_code)


@app.command("push-check", hidden=True)
def push_check(
    ctx: typer.Context,
    fail_on: FailOnOpt = FailOn.high,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Block the push when agentprint cannot check it. Same as AGENTPRINT_STRICT=1.",
        ),
    ] = False,
) -> None:
    """Git pre-push hook entry point (`agentprint protect` sets it up).

    Reads git's pre-push lines on stdin, checks what the push would publish, prints the brief
    and exits 1 to stop the push. A push that only deletes remote branches is never stopped.
    """
    from .protect import push_check as run_push_check

    platform.use_utf8_streams()
    refs = None if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    code, text = run_push_check(Path.cwd(), refs, os.environ, _given(ctx, fail_on), strict)
    typer.echo(text)
    raise typer.Exit(code)


@app.command()
def protect(
    path: Annotated[Path, typer.Argument(help="Project folder. Default: this one.")] = Path("."),
    undo: Annotated[
        bool, typer.Option("--undo", help="Take out what protect added, exactly.")
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="The hook blocks the push when agentprint is missing or cannot check it "
            "(default: it lets the push go with a note).",
        ),
    ] = False,
) -> None:
    """Check every git push and npm publish at git and npm themselves, for one project.

    Adds a git pre-push hook (a hook already there is kept and runs first) and, if PATH has a
    package.json, a prepublishOnly script. They fire however the push or publish was started,
    even one hidden from the agent hook. `agentprint protect --undo` takes them out.
    """
    from .protect import protect as run_protect
    from .protect import unprotect

    if undo and strict:
        typer.secho("error: --undo takes no --strict", err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    home = Path.home()
    code = (
        unprotect(path, home, typer.echo)
        if undo
        else run_protect(path, home, typer.echo, strict=strict)
    )
    raise typer.Exit(code)


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
def install(
    agent: AgentsOpt = None,
    dry_run: DryRunOpt = False,
    home: SetupHomeOpt = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Write hooks that block a publish or push agentprint cannot check "
            "(default: allow it with a warning).",
        ),
    ] = False,
) -> None:
    """Set agentprint up inside every AI coding agent on this computer.

    For each agent found it adds a hook that checks publishes and pushes (where the agent
    supports hooks), the agentprint MCP server, and a short note in the agent's global rules.
    Every change is shown as a diff and backed up first. `agentprint uninstall` undoes it.
    """
    from .install import install as run_install

    home_dir, env, names = _setup_args(agent, home)
    run_install(home_dir, env, names, dry_run, typer.echo, strict=strict)


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

    from .roster import roster, scanned, summary_line
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
    notes = [note for s in states if s.found and (note := _keep_one(s))]
    # Agents a sweep reads in full but install can't arm yet: listed when found.
    swept = scanned(home_dir)
    protected = _protected_here()  # only set in a protected repo: other output is unchanged
    if fmt is StatusFormat.json:
        agents = []
        for s in states:
            agent = {"name": s.name, "label": s.label, "found": s.found, "armed": s.armed}
            if s.plugin:  # only when one is there, so the usual output stays as it was
                agent["plugin"] = s.plugin  # "enabled", "disabled" or "unknown"
            agents.append(agent)
        also = [{"name": s.name, "label": s.label, "found": s.found} for s in swept]
        data = {"tool": "agentprint", "version": __version__, "agents": agents, "scanned": also}
        if protected:
            data["protected"] = protected
        typer.echo(json_lib.dumps(data))
    elif fmt is StatusFormat.agent:
        for s in states:
            typer.echo(f"{clean(s.name)} {_status_word(s)}")
        for s in swept:
            if s.found:
                typer.echo(f"{clean(s.name)} scanned (agentprint install can't arm it yet)")
        if any(s.found and not s.guarded for s in states):
            typer.echo("Arm unarmed agents with: agentprint install")
        for note in notes:
            typer.echo(note)
        if protected:
            typer.echo(_protected_text(protected))
    else:
        out = themed_console(env or dict(os.environ), plain=plain)
        out.print(Text(f"agentprint {__version__} · roster", style="ap.dim"))
        for line in hang("", summary_line(states, swept), out.width):
            out.print(line)
        shown = [s for s in [*states, *swept] if s.found]
        width = max([14, *(len(clean(s.label)) + 2 for s in shown)])
        for s in states:
            if s.found:
                line = Text(clean(s.label).ljust(width))
                line.append(_status_word(s), "ap.amber" if s.guarded else "ap.dim")
                out.print(line)
        for s in swept:
            if s.found:
                out.print(Text(clean(s.label).ljust(width)).append("scanned", "ap.dim"))
        if any(s.found for s in swept):
            note = "scanned = agentprint scan reads its files; agentprint install can't arm it yet"
            for line in hang("", Text(note, style="ap.dim"), out.width):
                out.print(line)
        missing = [clean(s.label) for s in states if not s.found]
        if missing:
            body = Text(", ".join(missing), style="ap.dim")
            for line in hang(Text("not found: ", style="ap.dim"), body, out.width):
                out.print(line)
        for note in notes:
            for line in hang("", note, out.width):
                out.print(line)
        if protected:
            for line in hang("", Text(_protected_text(protected), style="ap.dim"), out.width):
                out.print(line)
    return 0


def _protected_here() -> list[str]:
    """What checks pushes and publishes at git and npm themselves for the current folder."""
    from .protect import protection

    return protection(Path.cwd())


def _protected_text(found: list[str]) -> str:
    from .protect import protection_text

    return protection_text(found)


def _status_word(s: AgentState) -> str:
    from .plugins import DISABLED, ENABLED, UNKNOWN

    if not s.found:
        return "not found"
    if s.twice:
        return "armed twice (plugin and install)"
    if s.plugin == ENABLED:
        return "armed (plugin)"
    word = "armed" if s.armed else "unarmed"
    if s.plugin == UNKNOWN:
        return f"{word} (agentprint plugin found, not checked whether it is enabled)"
    if s.plugin == DISABLED:
        return f"{word} (agentprint plugin installed, not enabled)"
    return word


def _keep_one(s: AgentState) -> str:
    """What to do when agentprint may be in an agent both as a plugin and through install."""
    from .plugins import REMOVE_PLUGIN, UNKNOWN

    ways = (
        f"To keep the plugin, run: agentprint uninstall --agent {s.name}. To keep install, "
        f"remove the plugin: {REMOVE_PLUGIN.get(s.name, 'in the agent')}."
    )
    if s.twice:
        return (
            f"{s.label} has agentprint twice, as a plugin and through agentprint install: "
            f"keep one. {ways}"
        )
    if s.armed and s.plugin == UNKNOWN:
        return (
            f"{s.label} also has the agentprint plugin, and agentprint could not check whether "
            f"it is enabled. If it is, keep one. {ways}"
        )
    return ""


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
