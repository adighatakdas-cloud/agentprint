"""The bare-agentprint menu: keys, idle timeout, and the terminal always put back."""

import io
import os
import re
import sys

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


def test_numbered_list_on_windows_picks_with_a_digit():
    """Windows: a numbered list (type 1-7), read one key at a time with the same idle limit."""
    buf, con = _console()
    assert menu.choose(con, _reader(["3"]), 60.0, STATES, "~", numbered=True) == "install"
    out = _plain(buf)
    assert "agentprint scan · pack · install · status · --help" in out
    assert "1  Sweep this machine" in out and "7  Quit" in out
    assert "press 1-7 · q quit" in out
    assert out.rstrip().endswith("› Arm my agents")


@pytest.mark.parametrize("keys", [["enter"], ["quit"], ["7"], ["up", "down", "quit"]])
def test_numbered_list_other_keys_mean_quit(keys):
    _, con = _console()
    assert menu.choose(con, _reader(keys), 60.0, numbered=True) is None


def test_numbered_list_closes_after_idle():
    buf, con = _console()
    waits = []

    def reader(timeout):
        waits.append(timeout)
        return "down" if len(waits) == 1 else None  # arrows mean nothing in the list

    assert menu.choose(con, reader, 1.0, numbered=True) is None
    assert waits == [1.0, 1.0]
    assert "menu closed after 1 s idle · run agentprint again" in _plain(buf)


# ---- keys on Windows (msvcrt), with a stand-in module -----------------------------------------


class _FakeMsvcrt:
    def __init__(self, typed: str) -> None:
        self.keys = list(typed)

    def kbhit(self) -> bool:
        return bool(self.keys)

    def getwch(self) -> str:
        return self.keys.pop(0)


@pytest.fixture
def msvcrt(monkeypatch):
    def install(typed: str) -> _FakeMsvcrt:
        fake = _FakeMsvcrt(typed)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        return fake

    return install


@pytest.mark.parametrize(
    ("typed", "keys"),
    [
        ("\xe0H", ["up"]),
        ("\xe0P", ["down"]),
        ("\x00H", ["up"]),
        ("\x00P", ["down"]),
        ("k", ["up"]),
        ("j", ["down"]),
        ("\r", ["enter"]),
        ("q", ["quit"]),
        ("Q", ["quit"]),
        ("\x1b", ["quit"]),
        ("\x1a", ["quit"]),  # Ctrl-Z
        ("5", ["5"]),
        ("\xe0P\xe0P\r", ["down", "down", "enter"]),
        ("x9\xe0K\xe0M\x00;3", ["3"]),  # other keys, left/right and F1 are skipped
        ("\xe0H5", ["up", "5"]),  # an arrow's second half is never read as a key
    ],
)
def test_windows_reader_parses_keys(msvcrt, typed, keys):
    msvcrt(typed)
    with menu.windows_reader() as read_key:
        assert [read_key(1.0) for _ in keys] == keys
        assert read_key(0.05) is None


def test_windows_reader_times_out_without_a_key(msvcrt):
    import time

    msvcrt("")
    with menu.windows_reader() as read_key:
        started = time.monotonic()
        assert read_key(0.1) is None
        assert 0.09 <= time.monotonic() - started < 1.0


def test_windows_reader_ctrl_c_interrupts(msvcrt):
    msvcrt("\x03")
    with pytest.raises(KeyboardInterrupt), menu.windows_reader() as read_key:
        read_key(1.0)


def test_windows_reader_drops_unread_keys(msvcrt):
    fake = msvcrt("5xyz")
    with menu.windows_reader() as read_key:
        assert read_key(1.0) == "5"
    assert fake.keys == []  # typed but never read: not left for the next prompt


def test_key_reader_per_system(msvcrt):
    msvcrt("2")
    readers, numbered = menu.key_reader(None, "nt")
    assert numbered is True
    with readers as read_key:
        assert read_key(1.0) == "2"
    _, numbered = menu.key_reader(None, "posix")
    assert numbered is False


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
