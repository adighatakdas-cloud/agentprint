"""The bare-agentprint menu: keys, idle timeout, and the terminal always put back."""

import io
import os
import re

import pytest

from agentprint.roster import AgentState
from agentprint.ui import menu
from agentprint.ui.theme import console as themed_console

STATES = [
    AgentState("claude-code", "Claude Code", True, True),
    AgentState("cursor", "Cursor", True, False),
]
ANSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|[@-Z\\-_])")


@pytest.fixture(autouse=True)
def _real_terminal(monkeypatch):
    """The menu only ever runs in a real terminal (TERM=dumb never gets here): pin one."""
    monkeypatch.setenv("TERM", "xterm-256color")


def _reader(keys):
    """A fake KeyReader: hands out `keys` in order, then None (no key before the timeout)."""
    keys = iter(keys)
    return lambda timeout: next(keys, None)


def _console(width=100):
    buf = io.StringIO()
    return buf, themed_console({}, file=buf, force_terminal=True, width=width)


def _plain(buf):
    return ANSI.sub("", buf.getvalue())


def _choose(keys, idle=60.0, width=100):
    buf, con = _console(width)
    return menu.choose(con, _reader(keys), idle, STATES, "~/work/my-app"), _plain(buf)


def test_items_match_the_brief():
    assert [action for action, _, _ in menu.ITEMS] == [
        "scan",
        "pack",
        "install",
        "uninstall",
        "status",
        "help",
        "quit",
    ]
    assert menu.ITEMS[2] == (
        "install",
        "Arm my agents",
        "add the tripwire to every agent (shows the changes first)",
    )


def test_down_down_enter_picks_install():
    assert _choose(["down", "down", "enter"])[0] == "install"


def test_up_from_the_top_wraps_to_quit():
    assert _choose(["up", "enter"])[0] is None
    assert _choose(["up", "up", "enter"])[0] == "help"


def test_digit_picks_directly():
    assert _choose(["5"])[0] == "status"
    assert _choose(["down", "1"])[0] == "scan"


def test_quit_returns_none():
    assert _choose(["quit"])[0] is None
    assert _choose(["7"])[0] is None  # the Quit row


def test_unknown_keys_are_ignored():
    assert _choose(["9", "0", "left", "down", "enter"])[0] == "pack"


def test_idle_timeout_returns_none_and_says_so():
    buf, con = _console()
    waits = []

    def reader(timeout):
        waits.append(timeout)
        return "down" if len(waits) == 1 else None

    assert menu.choose(con, reader, 1.0) is None
    assert waits == [1.0, 1.0]  # every key starts the idle clock again
    assert "menu closed after 1 s idle · run agentprint again" in _plain(buf)


def test_ctrl_c_in_menu_stops_live_and_shows_cursor():
    buf, con = _console()

    def ctrl_c(timeout):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        menu.choose(con, ctrl_c, 60.0)
    raw = buf.getvalue()
    assert raw.rfind("\x1b[?25h") > raw.rfind("\x1b[?25l") >= 0  # cursor hidden, then shown


def test_commands_printed_before_menu():
    _, out = _choose(["quit"])
    assert "agentprint scan · pack · install · status · --help" in out
    assert out.index("agentprint scan") < out.index("Sweep this machine")
    assert out.index("1 of 2 agents armed") < out.index("Sweep this machine")
    assert out.index("~/work/my-app · offline") < out.index("Sweep this machine")


def test_picked_row_is_echoed_after_the_menu():
    _, out = _choose(["down", "enter"])
    assert out.rstrip().endswith("› Preflight a package")


def test_current_row_is_amber_and_strong_others_dim():
    rows = menu.rows(1)
    pointer, label, help_ = rows[1]
    assert (pointer.plain, pointer.style) == ("›", "ap.amber")
    assert (label.plain, label.style) == ("Preflight a package", "ap.strong")
    assert help_.style == "ap.dim"
    assert rows[0][0].plain.strip() == ""
    assert rows[0][1].style == "ap.dim"


@pytest.mark.parametrize("width", [40, 80, 120])
def test_menu_fits_any_width(width):
    _, out = _choose(["down", "enter"], width=width)
    assert "Sweep this machine" in out


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 60.0), ("1", 1.0), ("2.5", 2.5), ("abc", 60.0), ("0", 60.0), ("-5", 60.0)]
    + [("nan", 60.0), ("inf", 60.0), ("86400", 60.0)],
)
def test_idle_seconds(value, expected):
    env = {} if value is None else {"AGENTPRINT_MENU_IDLE": value}
    assert menu.idle_seconds(env) == expected


def test_numbered_prompt_without_a_key_reader(monkeypatch):
    """Windows has no termios: a numbered list and input()."""
    buf, con = _console()
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")
    assert menu.choose(con, None, 60.0, STATES, "~") == "install"
    out = _plain(buf)
    assert "agentprint scan · pack · install · status · --help" in out
    assert "1  Sweep this machine" in out and "7  Quit" in out


@pytest.mark.parametrize("answer", ["", "x", "0", "8", "7"])
def test_numbered_prompt_other_answers_mean_quit(monkeypatch, answer):
    buf, con = _console()
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert menu.choose(con, None, 60.0) is None


def test_numbered_prompt_eof_means_quit(monkeypatch):
    buf, con = _console()

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert menu.choose(con, None, 60.0) is None


# ---- the real key reader, on a pseudo-terminal ------------------------------------------------

posix = pytest.mark.skipif(os.name != "posix", reason="termios is POSIX only")


@pytest.fixture
def tty():
    import pty

    master, slave = pty.openpty()
    yield master, slave
    os.close(master)
    os.close(slave)


class _Stream:
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


@posix
@pytest.mark.parametrize(
    ("typed", "keys"),
    [
        (b"\x1b[A", ["up"]),
        (b"\x1b[B", ["down"]),
        (b"\x1bOA\x1bOB", ["up", "down"]),
        (b"\x1b[1;5B", ["down"]),
        (b"k", ["up"]),
        (b"j", ["down"]),
        (b"\r", ["enter"]),
        (b"\n", ["enter"]),
        (b"q", ["quit"]),
        (b"Q", ["quit"]),
        (b"\x04", ["quit"]),
        (b"\x1b", ["quit"]),
        (b"5", ["5"]),
        (b"\x1b[B\x1b[B\r", ["down", "down", "enter"]),
        (b"x9\x1b[C\x1b[200~3", ["3"]),  # other keys, arrows and sequences are skipped
    ],
)
def test_reader_parses_keys(tty, typed, keys):
    master, slave = tty
    with menu.posix_reader(_Stream(slave)) as read_key:
        os.write(master, typed)
        assert [read_key(1.0) for _ in keys] == keys
        assert read_key(0.05) is None


@posix
def test_reader_times_out_with_none(tty):
    _, slave = tty
    with menu.posix_reader(_Stream(slave)) as read_key:
        assert read_key(0.05) is None


@posix
def test_reader_cbreak_inside_and_restored_after(tty):
    import termios

    _, slave = tty
    before = termios.tcgetattr(slave)
    with menu.posix_reader(_Stream(slave)):
        inside = termios.tcgetattr(slave)
        assert not inside[3] & termios.ICANON and not inside[3] & termios.ECHO
        assert inside[3] & termios.ISIG  # Ctrl-C still interrupts
    assert termios.tcgetattr(slave) == before


@posix
def test_reader_restores_terminal_on_interrupt(tty, monkeypatch):
    """Review focus 2: Ctrl-C while waiting for a key puts the terminal back."""
    import termios

    _, slave = tty
    before = termios.tcgetattr(slave)
    calls = []
    real = termios.tcsetattr

    def spy(fd, when, attrs):
        calls.append(attrs)
        real(fd, when, attrs)

    def ctrl_c(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(termios, "tcsetattr", spy)
    monkeypatch.setattr(menu.select, "select", ctrl_c)
    with pytest.raises(KeyboardInterrupt), menu.posix_reader(_Stream(slave)) as read_key:
        read_key(1.0)
    assert calls[-1] == before
    assert termios.tcgetattr(slave) == before


def test_module_imports_without_termios():
    """Windows has no termios: importing the menu and the CLI must not need it."""
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['termios'] = None; sys.modules['tty'] = None\n"
        "import agentprint.ui.menu, agentprint.cli\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)


@pytest.mark.parametrize("width", [64, 80, 100])
def test_menu_rows_line_up_with_the_header(width):
    """macOS Terminal opens 80 columns wide: the rows keep the header's 10-column indent."""
    _, out = _choose(["quit"], width=width)
    lines = [ln.rstrip() for ln in out.replace("\r", "\n").splitlines()]
    assert any(ln.startswith(menu.INDENT + "agentprint scan · pack") for ln in lines)
    assert any(ln.startswith(menu.INDENT + "› Sweep this machine") for ln in lines)
    assert any(ln.startswith(menu.INDENT + "  Preflight a package") for ln in lines)
    assert any(ln.startswith(menu.INDENT + "↑↓ move") for ln in lines)
    assert all(len(ln) <= width for ln in lines), "\n".join(lines)


def test_menu_help_wraps_in_its_column():
    _, out = _choose(["quit"], width=80)
    lines = [ln.rstrip() for ln in out.replace("\r", "\n").splitlines()]
    row = next(i for i, ln in enumerate(lines) if "Arm my agents" in ln)
    help_col = lines[row].index("add the tripwire")
    assert lines[row + 1][:help_col].strip() == ""  # the rest of the help, under its column
    assert lines[row + 1].strip() == "changes first)"


def test_very_narrow_menu_drops_the_indent():
    _, out = _choose(["quit"], width=40)
    lines = [ln.rstrip() for ln in out.replace("\r", "\n").splitlines()]
    assert any(ln.startswith("› Sweep this machine") for ln in lines)
