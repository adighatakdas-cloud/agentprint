"""Bare `agentprint`: help for pipes, CI and AI agents; welcome once, then the menu, for people."""

import os
import re
import select
import signal
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import pytest
import typer
import typer.main
import typer.rich_utils
from typer.testing import CliRunner

from agentprint import __version__, cli
from agentprint.cli import app
from agentprint.report import FailOn
from agentprint.ui import menu
from agentprint.ui.welcome import mark_welcome, seen_welcome

runner = CliRunner()
ANSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|[@-Z\\-_])")
posix = pytest.mark.skipif(os.name != "posix", reason="pseudo-terminals are POSIX only")


def test_bare_without_tty_prints_help_exit_0():
    """No terminal: help, exit 0. (The pty tests below prove it never waits on stdin.)"""
    r = runner.invoke(app, [])
    assert r.exit_code == 0, r.output
    assert "Usage" in r.output
    assert "scan" in r.output and "install" in r.output


def test_bare_with_agent_marker_prints_help(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(cli, "should_animate", lambda *a, **k: pytest.fail("no animation"))
    r = runner.invoke(app, [])
    assert r.exit_code == 0 and "Usage" in r.output


def test_help_and_version_unchanged():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0 and "Usage" in r.output and "Commands" in r.output
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and r.output.strip() == f"agentprint {__version__}"


def test_subcommands_do_not_open_the_menu(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "is_interactive", lambda *a: True)
    monkeypatch.setattr(menu, "choose", lambda *a, **k: pytest.fail("menu opened"))
    r = runner.invoke(app, ["status", "--home", str(tmp_path), "--format", "agent"])
    assert r.exit_code == 0


# ---- a person at a terminal (is_interactive patched; the menu itself is tested elsewhere) -----


@pytest.fixture
def human(monkeypatch, tmp_path):
    """A person at a terminal whose home (a tmp folder) has already seen the welcome."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "is_interactive", lambda *a: True)
    monkeypatch.setattr(menu, "posix_reader", lambda stream: nullcontext(lambda timeout: None))
    mark_welcome(home)
    return home


def _pick(monkeypatch, action):
    seen = {}

    def choose(console, reader, idle, states=(), cwd=".", light=False, numbered=False):
        seen.update(reader=reader, idle=idle, states=states, cwd=cwd, numbered=numbered)
        if isinstance(action, BaseException):
            raise action
        return action

    monkeypatch.setattr(menu, "choose", choose)
    return seen


def test_first_run_plays_welcome_marks_and_exits(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "is_interactive", lambda *a: True)
    monkeypatch.setattr(menu, "choose", lambda *a, **k: pytest.fail("menu on first run"))
    r = runner.invoke(app, [])
    assert r.exit_code == 0, r.output
    assert "The security check your AI coding agent runs for you." in r.output
    assert seen_welcome(home)


def test_second_run_opens_the_menu_with_real_roster(human, monkeypatch):
    (human / ".claude").mkdir()
    monkeypatch.setenv("AGENTPRINT_MENU_IDLE", "7")
    seen = _pick(monkeypatch, None)
    r = runner.invoke(app, [])
    assert r.exit_code == 0, r.output
    assert seen["idle"] == 7.0
    assert any(s.name == "claude-code" and s.found for s in seen["states"])
    assert "The security check" not in r.output


def test_menu_scan_runs_the_scan_command(human, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_scan_command", lambda *a: calls.append(a) or 1)
    _pick(monkeypatch, "scan")
    r = runner.invoke(app, [])
    assert calls == [(Path("."), None, False, None, FailOn.high, False)]
    assert r.exit_code == 1  # the same exit code as `agentprint scan .`


def test_menu_pack_runs_the_pack_command(human, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_pack_command", lambda *a: calls.append(a) or 0)
    _pick(monkeypatch, "pack")
    r = runner.invoke(app, ["--plain"])
    assert calls == [(Path("."), None, FailOn.high, True)]
    assert r.exit_code == 0


@pytest.mark.parametrize("marker", ["pyproject.toml", "setup.py"])
def test_menu_pack_of_a_python_project_asks_first_and_no_is_the_default(
    human, monkeypatch, tmp_path, marker
):
    """Preflighting a Python project runs `uv build` (its build code, maybe a download)."""
    (tmp_path / marker).write_text("")
    calls = []
    monkeypatch.setattr(cli, "run_pack_command", lambda *a: calls.append(a) or 1)
    _pick(monkeypatch, "pack")
    r = runner.invoke(app, [], input="\n")
    assert r.exit_code == 0, r.output
    assert calls == []
    assert "uv build" in r.output and "Build and preflight it? [y/N]" in r.output
    assert "agentprint pack" in r.output  # how to check a built package instead


def test_menu_pack_of_a_python_project_yes_runs_it(human, monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    calls = []
    monkeypatch.setattr(cli, "run_pack_command", lambda *a: calls.append(a) or 1)
    _pick(monkeypatch, "pack")
    r = runner.invoke(app, [], input="y\n")
    assert calls == [(Path("."), None, FailOn.high, False)]
    assert r.exit_code == 1


def test_menu_pack_of_an_npm_project_does_not_ask(human, monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}")
    calls = []
    monkeypatch.setattr(cli, "run_pack_command", lambda *a: calls.append(a) or 0)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: pytest.fail("asked for npm"))
    _pick(monkeypatch, "pack")
    assert runner.invoke(app, []).exit_code == 0
    assert len(calls) == 1


def test_menu_says_preflight_asks_before_a_python_build():
    assert "asks before a Python build" in dict((a, h) for a, _, h in menu.ITEMS)["pack"]


def _fake_setup(monkeypatch, which, changes):
    calls = []

    def run(home, env, names, dry_run, echo, **kwargs):
        calls.append({"home": home, "env": env, "names": names, "dry_run": dry_run, **kwargs})
        echo(f"fake {which} dry_run={dry_run}")
        return changes

    monkeypatch.setattr(f"agentprint.install.{which}", run)
    return calls


@pytest.mark.parametrize(
    ("action", "question"),
    [("install", "Arm these agents?"), ("uninstall", "Disarm these agents?")],
)
def test_menu_setup_shows_dry_run_then_asks_no_means_nothing(human, monkeypatch, action, question):
    calls = _fake_setup(monkeypatch, action, 2)
    _pick(monkeypatch, action)
    r = runner.invoke(app, [], input="\n")  # default is No
    assert r.exit_code == 0, r.output
    assert [c["dry_run"] for c in calls] == [True]
    assert r.output.index("dry_run=True") < r.output.index(question)
    assert f"{question} [y/N]" in r.output


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_menu_setup_yes_runs_for_real_with_real_home_and_env(human, monkeypatch, action):
    monkeypatch.setenv("CODEX_HOME", str(human / "codex-elsewhere"))
    calls = _fake_setup(monkeypatch, action, 2)
    _pick(monkeypatch, action)
    r = runner.invoke(app, [], input="y\n")
    assert r.exit_code == 0, r.output
    assert [c["dry_run"] for c in calls] == [True, False]
    for c in calls:
        assert c["home"] == human and c["names"] is None
        assert c["env"]["CODEX_HOME"] == str(human / "codex-elsewhere")


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_menu_dry_run_drops_the_command_line_hint(human, monkeypatch, action):
    """The menu asks y/N itself: "Run without --dry-run to apply." would contradict it."""
    calls = _fake_setup(monkeypatch, action, 2)
    _pick(monkeypatch, action)
    runner.invoke(app, [], input="\n")
    assert calls[0]["dry_run"] is True and calls[0]["apply_hint"] is None


def test_apply_hint_is_optional_and_on_by_default(tmp_path):
    from agentprint import install

    (tmp_path / ".claude").mkdir()
    default, quiet = [], []
    install.install(tmp_path, {}, None, True, default.append)
    install.install(tmp_path, {}, None, True, quiet.append, apply_hint=None)
    assert default[-1 - _notes(default)].endswith("would change. Run without --dry-run to apply.")
    assert quiet[-1 - _notes(quiet)].endswith("file(s) would change.")
    for kwargs in ({}, {"apply_hint": None}):
        out = []
        assert install.uninstall(tmp_path, {}, None, True, out.append, **kwargs) == 0
        hinted = out[-1].endswith("Run without --dry-run to apply.")
        assert hinted is ("apply_hint" not in kwargs)


def _notes(lines):
    """How many "Note for ..." lines end install's output."""
    return sum(1 for line in lines if line.startswith("Note for "))


def test_menu_setup_nothing_to_change_does_not_ask(human, monkeypatch):
    calls = _fake_setup(monkeypatch, "install", 0)
    _pick(monkeypatch, "install")
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: pytest.fail("asked anyway"))
    r = runner.invoke(app, [])
    assert r.exit_code == 0, r.output
    assert [c["dry_run"] for c in calls] == [True]
    assert "Arm these agents?" not in r.output


def test_install_dry_run_counts_the_files_it_would_change(tmp_path):
    from agentprint import install

    (tmp_path / ".claude").mkdir()
    out = []
    assert install.install(tmp_path, {}, None, True, out.append) > 0
    assert install.uninstall(tmp_path, {}, None, True, out.append) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert install.install(empty, {}, None, True, out.append) == 0


def test_menu_status_prints_the_roster(human, monkeypatch):
    _pick(monkeypatch, "status")
    r = runner.invoke(app, [])
    assert r.exit_code == 0
    assert f"agentprint {__version__} · roster" in r.output


def test_menu_help_prints_usage(human, monkeypatch):
    _pick(monkeypatch, "help")
    r = runner.invoke(app, [])
    assert r.exit_code == 0 and "Usage" in r.output


# ---- help: plain text for AI agents and pipes; typer's colors only for a person at a terminal --


def test_menu_help_into_a_pipe_is_plain_even_when_color_is_forced(human, monkeypatch):
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", True)  # as under GITHUB_ACTIONS
    _pick(monkeypatch, "help")
    r = runner.invoke(app, [])
    assert r.exit_code == 0 and "Usage" in r.output
    assert "\x1b[" not in r.output


def test_menu_help_with_plain_is_plain_at_a_terminal(human, monkeypatch):
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setattr(cli, "plain_help", lambda *a: False)  # a person at a real terminal
    _pick(monkeypatch, "help")
    r = runner.invoke(app, ["--plain"])
    assert r.exit_code == 0 and "Usage" in r.output
    assert "\x1b[" not in r.output


def test_rich_help_at_a_terminal_adds_no_blank_line(human, monkeypatch):
    """Typer's rich help prints itself and get_help() returns "": nothing more to echo."""
    monkeypatch.setattr(cli, "plain_help", lambda *a: False)  # a person at a real terminal
    _pick(monkeypatch, "help")
    r = runner.invoke(app, [])
    assert "Usage" in r.output and not r.output.endswith("\n\n")


def test_bare_help_ends_without_a_blank_line():
    r = runner.invoke(app, [])
    assert "Usage" in r.output and r.output.endswith("\n") and not r.output.endswith("\n\n")


def test_every_command_help_is_plain_when_typer_would_force_color(monkeypatch):
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", True)  # as under GITHUB_ACTIONS
    names = list(typer.main.get_command(app).commands)
    assert {"scan", "pack", "install", "status"} <= set(names)
    for args in ([], *([name] for name in names)):
        r = runner.invoke(app, [*args, "--help"])
        assert r.exit_code == 0 and "Usage" in r.output, args
        assert "\x1b[" not in r.output, args


FORCED = {"GITHUB_ACTIONS": "true", "FORCE_COLOR": "1"}  # typer's console forces color for these


def _piped(home: Path, args: list[str], **extra: str) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), **FORCED, **extra}
    return subprocess.run([*CHILD, *args], env=env, cwd=home, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=30)  # fmt: skip


@pytest.mark.parametrize("args", [[], ["--help"], ["scan", "--help"]], ids=["bare", "help", "scan"])
@pytest.mark.parametrize("agent", [{}, {"CLAUDECODE": "1"}], ids=["pipe", "agent"])
def test_help_into_a_pipe_has_no_escape_codes_even_when_color_is_forced(tmp_path, args, agent):
    r = _piped(tmp_path, args, **agent)
    assert r.returncode == 0, r.stderr
    assert b"Usage" in r.stdout
    assert b"\x1b[" not in r.stdout + r.stderr


def test_usage_error_for_an_agent_has_no_escape_codes(tmp_path):
    r = _piped(tmp_path, ["scan", "--no-such-option"], CLAUDECODE="1")
    assert r.returncode == 2
    assert b"No such option" in r.stderr
    assert b"\x1b[" not in r.stdout + r.stderr


@pytest.mark.parametrize("action", [None, "quit"])
def test_menu_quit_exits_0(human, monkeypatch, action):
    monkeypatch.setattr(cli, "run_scan_command", lambda *a: pytest.fail("scan ran"))
    _pick(monkeypatch, action)
    r = runner.invoke(app, [])
    assert r.exit_code == 0 and r.output == ""


def test_ctrl_c_in_menu_exits_130_without_traceback(human, monkeypatch):
    _pick(monkeypatch, KeyboardInterrupt())
    r = runner.invoke(app, [])
    assert r.exit_code == 130
    assert "Traceback" not in r.output and "Aborted" not in r.output


def test_ctrl_c_during_first_run_welcome_exits_130(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "is_interactive", lambda *a: True)

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("agentprint.ui.sweep.play", boom)
    r = runner.invoke(app, [])
    assert r.exit_code == 130 and "Traceback" not in r.output


def test_ctrl_c_at_the_question_exits_130(human, monkeypatch):
    _fake_setup(monkeypatch, "install", 1)
    _pick(monkeypatch, "install")

    def abort(*a, **k):
        raise typer.Abort

    monkeypatch.setattr(typer, "confirm", abort)
    r = runner.invoke(app, [])
    assert r.exit_code == 130
    assert "Traceback" not in r.output and "Aborted" not in r.output


# ---- the real thing, in a pseudo-terminal (a fake terminal window) ----------------------------

CHILD = [sys.executable, "-c", "from agentprint.cli import app; app(prog_name='agentprint')"]


def _env(home: Path, **extra: str) -> dict[str, str]:
    """Built from scratch: no CI, NO_COLOR or AI-agent markers leak in from the test runner."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "TERM": "xterm-256color",
        **extra,
    }


class _Pty:
    """Runs agentprint on a pseudo-terminal and reads its screen with a deadline."""

    def __init__(self, home: Path, env: dict[str, str], stdin_tty=True, stdout_tty=True, args=()):
        import fcntl
        import pty
        import struct
        import termios

        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.before = termios.tcgetattr(self.slave)
        out = self.slave if stdout_tty else subprocess.PIPE
        self.proc = subprocess.Popen(
            [*CHILD, *args],
            stdin=self.slave if stdin_tty else subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            env=env,
            cwd=home,
        )
        self.raw = bytearray()

    @property
    def screen(self) -> str:
        return ANSI.sub("", self.raw.decode("utf-8", "replace"))

    def read(self, until: str | None = None, timeout: float = 5.0) -> str:
        """Read until `until` is on screen, or the child exits. A hang fails the test."""
        deadline = time.monotonic() + timeout
        while until is None or until not in self.screen:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.proc.kill()
                self.proc.wait()
                pytest.fail(f"agentprint still running after {timeout} s; screen:\n{self.screen}")
            ready, _, _ = select.select([self.master], [], [], min(remaining, 0.05))
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:  # Linux: EIO once nothing holds the terminal
                    chunk = b""
                self.raw += chunk
                if chunk:
                    continue
            if self.proc.poll() is not None:
                break
        return self.screen

    def wait(self, timeout: float = 5.0) -> int:
        self.read(timeout=timeout)
        return self.proc.wait(timeout=1)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def run_pty():
    ptys = []

    def start(home, env, **kw):
        p = _Pty(home, env, **kw)
        ptys.append(p)
        return p

    yield start
    for p in ptys:
        p.close()


@pytest.fixture
def welcomed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    mark_welcome(home)
    return home


@posix
def test_bare_with_agent_marker_in_pty_prints_help_fast(tmp_path, run_pty):
    started = time.monotonic()
    p = run_pty(tmp_path, _env(tmp_path, CLAUDECODE="1"))
    assert p.wait() == 0
    assert time.monotonic() - started < 5
    assert "Usage" in p.screen
    assert not seen_welcome(tmp_path)  # an AI agent is not a person seeing the welcome


@posix
def test_help_for_an_agent_in_a_real_terminal_has_no_escape_codes(tmp_path, run_pty):
    p = run_pty(tmp_path, _env(tmp_path, CLAUDECODE="1", **FORCED), args=["--help"])
    assert p.wait() == 0
    assert "Usage" in p.screen and b"\x1b[" not in p.raw


@posix
def test_help_for_a_person_at_a_terminal_keeps_typer_colors(tmp_path, run_pty):
    p = run_pty(tmp_path, _env(tmp_path), args=["--help"])
    assert p.wait() == 0
    assert "Usage" in p.screen and b"\x1b[" in p.raw


@posix
def test_bare_in_pty_idle_menu_exits(welcomed, run_pty):
    p = run_pty(welcomed, _env(welcomed, AGENTPRINT_MENU_IDLE="1"))
    assert p.wait() == 0
    screen = p.screen
    assert "agentprint scan · pack · install · status · --help" in screen
    assert "menu closed after 1 s idle · run agentprint again" in screen
    assert screen.index("agentprint scan · pack") < screen.index("Sweep this machine")
    assert p.raw.rfind(b"\x1b[?25h") > p.raw.rfind(b"\x1b[?25l")  # cursor shown again


@posix
def test_bare_first_run_in_pty_shows_welcome_and_marks(tmp_path, run_pty):
    p = run_pty(tmp_path, _env(tmp_path))
    assert p.wait() == 0
    assert "agentprint" in p.screen and __version__ in p.screen
    assert "Sweep this machine" not in p.screen  # the welcome only, no menu
    assert seen_welcome(tmp_path)


@posix
def test_piped_stdout_with_tty_stdin_prints_help(welcomed, run_pty):
    """Review focus 5: `agentprint | cat` in a terminal."""
    p = run_pty(welcomed, _env(welcomed), stdout_tty=False)
    out, _ = p.proc.communicate(timeout=5)
    assert p.proc.returncode == 0
    assert b"Usage" in out and b"\x1b[" not in out


@posix
def test_devnull_stdin_with_tty_stdout_prints_help(welcomed, run_pty):
    """Review focus 5: `agentprint < /dev/null` in a terminal."""
    p = run_pty(welcomed, _env(welcomed), stdin_tty=False)
    assert p.wait() == 0
    assert "Usage" in p.screen and "Sweep this machine" not in p.screen


@posix
def test_bare_with_stdin_closed_prints_help(welcomed):
    """`agentprint <&-`: Python sets sys.stdin to None; still help, no traceback."""
    shell = ["sh", "-c", 'exec "$0" "$@" <&-', *CHILD]
    r = subprocess.run(shell, env=_env(welcomed), capture_output=True, timeout=10)
    assert r.returncode == 0, r.stderr
    assert b"Usage" in r.stdout and b"Traceback" not in r.stderr


@posix
def test_bare_in_pty_key_picks_status(welcomed, run_pty):
    p = run_pty(welcomed, _env(welcomed, AGENTPRINT_MENU_IDLE="5"))
    p.read(until="q quit")
    os.write(p.master, b"5")
    assert p.wait() == 0
    assert f"agentprint {__version__} · roster" in p.screen


@posix
def test_bare_in_pty_arrows_then_enter(welcomed, run_pty):
    p = run_pty(welcomed, _env(welcomed, AGENTPRINT_MENU_IDLE="5"))
    p.read(until="q quit")
    os.write(p.master, b"\x1b[A\x1b[A\r")  # up, up: from Sweep to Help
    assert p.wait() == 0
    assert "› Help" in p.screen and "Usage" in p.screen


@posix
def test_ctrl_c_in_pty_menu_restores_terminal_exits_130(welcomed, run_pty):
    """Review focus 2: Ctrl-C in the menu: echo back on, cursor visible, 130, no traceback."""
    import termios

    p = run_pty(welcomed, _env(welcomed, AGENTPRINT_MENU_IDLE="5"))
    p.read(until="q quit")
    p.proc.send_signal(signal.SIGINT)
    assert p.wait() == 130
    assert "Traceback" not in p.screen
    assert termios.tcgetattr(p.slave) == p.before
    assert p.raw.rfind(b"\x1b[?25h") > p.raw.rfind(b"\x1b[?25l")
