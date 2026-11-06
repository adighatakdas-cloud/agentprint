import io

import pytest

from agentprint.ui.detect import AGENT_MARKERS, is_agent, is_interactive, should_animate


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("marker", AGENT_MARKERS)
def test_markers_detect_agents(marker):
    assert is_agent({marker: "1"})


def test_empty_marker_is_not_agent():
    assert not is_agent({"CLAUDECODE": ""})


CASES = [
    ("not-a-tty", {}, io.StringIO(), False),
    ("ci", {"CI": "1"}, FakeTTY(), False),
    ("no-color", {"NO_COLOR": "1"}, FakeTTY(), False),
    ("dumb", {"TERM": "dumb"}, FakeTTY(), False),
    ("plain", {}, FakeTTY(), True),
] + [(m, {m: "1"}, FakeTTY(), False) for m in AGENT_MARKERS]


@pytest.mark.parametrize(
    ("env", "stdout", "plain"),
    [pytest.param(e, s, p, id=i) for i, e, s, p in CASES],
)
def test_should_animate_false_when(env, stdout, plain):
    assert not should_animate(env, stdout, plain=plain)


def test_should_animate_true_for_plain_human():
    assert should_animate({}, FakeTTY())


def test_menu_needs_both_ttys():
    assert not is_interactive({}, io.StringIO(), FakeTTY())
    assert is_interactive({}, FakeTTY(), FakeTTY())


def test_no_color_keeps_menu():
    assert is_interactive({"NO_COLOR": "1"}, FakeTTY(), FakeTTY())


@pytest.mark.parametrize("env", [{"CI": "1"}, {"TERM": "dumb"}, {"CLAUDECODE": "1"}])
def test_menu_off_for_ci_dumb_and_agents(env):
    assert not is_interactive(env, FakeTTY(), FakeTTY())


@pytest.mark.parametrize("missing", ["stdin", "stdout"])
def test_closed_or_missing_stream_is_not_a_terminal(missing):
    """`agentprint <&-` leaves sys.stdin as None; a closed file raises on isatty()."""
    closed = io.StringIO()
    closed.close()  # isatty() now raises ValueError
    for gone in (None, closed):
        streams = {"stdin": FakeTTY(), "stdout": FakeTTY(), missing: gone}
        assert not is_interactive({}, streams["stdin"], streams["stdout"])
    assert not should_animate({}, None)
    assert not should_animate({}, closed)
