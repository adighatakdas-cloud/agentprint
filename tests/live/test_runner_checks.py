"""The live tests' own checks and the shell probe (fast; runs in the normal suite)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.live import harness
from tests.live.runner import (
    Outcome,
    assert_help_not_menu,
    assert_plain_text,
    assert_scan_output,
    shell_probe,
    texts,
)


def outcome(*results: str) -> Outcome:
    return Outcome(False, False, list(results), "", "", Path("."))


def test_probe_prints_names_never_values():
    env = {"PATH": os.environ.get("PATH", ""), "CLAUDECODE": "s3cret-value", "NO_COLOR": "1",
           "GEMINI_CLI": ""}  # fmt: skip
    r = subprocess.run(["sh", "-c", harness.PROBE], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=30)  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "s3cret-value" not in r.stdout
    assert harness.PROBE_TAG not in harness.PROBE  # the command itself never looks like output
    assert shell_probe(outcome(r.stdout)) == {
        "markers": ["CLAUDECODE"],
        "color": ["NO_COLOR"],
        "stdin_tty": False,
        "stdout_tty": False,
    }


def test_wrapped_results_are_unwrapped():
    gemini = json.dumps({"output": "agentprint: OK\nmore"})
    hermes = json.dumps({"output": "Usage: agentprint", "exit_code": 0, "error": None})
    assert texts(outcome(gemini, hermes, "plain", "3")) == [
        "agentprint: OK\nmore",
        "Usage: agentprint",
        "plain",
        "3",
    ]


@pytest.mark.parametrize(
    "result",
    ["\x1b[1mUsage\x1b[0m", json.dumps({"output": "\x1b[1mUsage"}), "out: '\\x1b[1m'"],
)
def test_escape_codes_are_caught_raw_or_wrapped(result):
    with pytest.raises(AssertionError, match="escape codes"):
        assert_plain_text(outcome(result))


def test_no_output_at_all_is_not_plain_text():
    with pytest.raises(AssertionError, match="never got"):
        assert_plain_text(outcome())


AGENT = "agentprint: OK"
PERSON = "agentprint scan  . · 0.01s\n\n CLEAR   no prints · keys masked"


@pytest.mark.parametrize(
    "result", [AGENT, "Exit code: 0\nOutput:\nagentprint: OK", "Output: agentprint: OK"]
)
def test_scan_agent_format_found(result):
    assert_scan_output(outcome(result), agent_format=True)
    with pytest.raises(AssertionError, match="person's layout"):
        assert_scan_output(outcome(result), agent_format=False)


def test_scan_person_layout_found():
    assert_scan_output(outcome(PERSON), agent_format=False)
    with pytest.raises(AssertionError, match="verdict"):
        assert_scan_output(outcome(PERSON), agent_format=True)


def test_help_not_menu():
    assert_help_not_menu(outcome("Usage: agentprint [OPTIONS] COMMAND [ARGS]..."))
    with pytest.raises(AssertionError, match="menu opened"):
        assert_help_not_menu(outcome("Usage\n› Sweep this machine\nmenu closed after 60 s idle"))
    with pytest.raises(AssertionError, match="no help"):
        assert_help_not_menu(outcome("agentprint: OK"))
