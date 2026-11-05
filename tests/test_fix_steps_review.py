"""Fix steps are never shortened: a cut shell command can run part of a file name (review #1)."""

import json
import shlex

import pytest
from rich.console import Console

from agentprint.advice import Moment, advise
from agentprint.models import Finding, Severity
from agentprint.report import FailOn, print_table, to_agent, to_json
from agentprint.sanitize import FIX_LEN, SAFE_STEP
from agentprint.sarif import to_sarif

PAYLOAD = "$(echo PWNED>&2)"
NAMES = [f"x'{PAYLOAD}\"'{i}" + "y" * 300 + ".json" for i in range(5)]
LONG = "chmod 600 " + " ".join(shlex.quote("/home/u/repo/.github/hooks/" + n) for n in NAMES)
ESCAPED = "chmod 600 " + shlex.quote("/x/\x1b['A" + PAYLOAD + ".json")  # clean() would cut a '


def _finding(check_id: str, fix: str) -> Finding:
    return Finding(check_id, Severity.HIGH, "t", "a", "/x/f.json", None, "d", fix)


def _outputs(finding: Finding) -> dict[str, str]:
    console = Console(record=True, width=100_000)
    print_table([finding], console)
    return {
        "table": console.export_text(styles=False),
        "json": to_json([finding], Moment.scan),
        "agent": to_agent([finding], FailOn.high, Moment.scan),
        "sarif": to_sarif([finding]),
    }


@pytest.mark.parametrize("check_id", ["AP999", "AP008", "AP007"])  # fallback, and two checks
@pytest.mark.parametrize("fix", [LONG, ESCAPED], ids=["too-long", "needs-cleaning"])
def test_a_command_is_shown_whole_or_not_at_all(check_id, fix):
    assert len(LONG) > FIX_LEN
    outputs = _outputs(_finding(check_id, fix))
    for name, text in outputs.items():
        assert "chmod 600" not in text, name  # never a cut (or altered) command
    assert SAFE_STEP in outputs["table"]  # these render the check's own fix text
    assert json.loads(outputs["json"])["findings"][0]["fix"] == SAFE_STEP
    sarif_help = json.loads(outputs["sarif"])["runs"][0]["tool"]["driver"]["rules"][0]["help"]
    assert sarif_help["text"] == SAFE_STEP


def test_safe_step_is_plain_words_without_any_command():
    assert all(ch not in SAFE_STEP for ch in "$`'\";|&<>")


def test_short_clean_commands_are_kept_exactly():
    fix = "chmod 600 '/home/u/.claude/settings.local.json'"
    assert advise(_finding("AP999", fix), Moment.scan).fix_steps == [fix]
    assert fix in _outputs(_finding("AP999", fix))["agent"]
