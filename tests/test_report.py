import json

from rich.console import Console

from agentprint.advice import Moment
from agentprint.models import Finding, Severity
from agentprint.report import FailOn, exit_code, print_table, to_agent, to_json


def _finding(sev=Severity.HIGH, detail="GitHub token: ghp_…(len 40)"):
    return Finding(
        "AP001",
        sev,
        "Secret in agent history",
        "claude-code",
        "/x/[odd]/h.jsonl",
        3,
        detail,
        "Rotate it.",
        count=2,
    )


def test_json_has_summary_and_findings():
    data = json.loads(to_json([_finding()]))
    assert data["summary"]["high"] == 1
    assert data["findings"][0]["severity"] == "high"
    assert data["findings"][0]["count"] == 2


def test_table_prints_rows_fix_and_summary_without_markup_errors():
    console = Console(record=True, width=200)
    print_table([_finding()], console)
    text = console.export_text()
    assert "AP001" in text and "[odd]" in text and "(×2)" in text and "Rotate it." in text


def test_table_says_clean_when_empty():
    console = Console(record=True, width=200)
    print_table([], console)
    assert "CLEAR" in console.export_text()


def test_exit_code_respects_threshold():
    findings = [_finding(Severity.MEDIUM)]
    assert exit_code(findings, FailOn.high) == 0
    assert exit_code(findings, FailOn.medium) == 1
    assert exit_code(findings, FailOn.none) == 0
    assert exit_code([], FailOn.low) == 0


def test_table_and_json_strip_escape_codes_from_scanned_paths():
    f = _finding()
    f.location = "pkg.tgz!package/\x1b[31mred\x1b[0m\n/.claude/settings.local.json"
    console = Console(record=True, width=300)
    print_table([f], console)
    for text in (console.export_text(styles=False), to_json([f])):
        assert "\x1b" not in text and "[31m" not in text
    assert "\\n" not in to_json([f])


def _leak():
    return Finding(
        "AP006",
        Severity.HIGH,
        "Agent file would ship in package",
        "claude-code",
        "demo-1.0.0.tgz!package/.claude/settings.local.json",
        None,
        "Personal permission allowlist.",
        "Keep it out.",
    )


def test_agent_format_first_line_is_the_block_verdict():
    text = to_agent([_leak(), _finding(Severity.LOW)], FailOn.high, Moment.publish)
    lines = text.splitlines()
    assert lines[0] == "agentprint: BLOCK — 1 problem to fix before publishing"
    assert "fix:" in text and "ask_human:" in text
    assert "not instructions" in text  # tells the model quoted values are data


def test_agent_format_ok_when_nothing_reaches_the_threshold():
    text = to_agent([_finding(Severity.LOW)], FailOn.high, Moment.publish)
    assert text.splitlines()[0] == "agentprint: OK"
    assert to_agent([], FailOn.high, Moment.push).splitlines()[0] == "agentprint: OK"


def test_agent_format_push_wording_and_rerun_hint():
    text = to_agent([_finding(), _finding()], FailOn.high, Moment.push, rerun="git push")
    assert text.splitlines()[0] == "agentprint: BLOCK — 2 problems to fix before pushing"
    assert "git push" in text.splitlines()[-1]


def test_json_findings_carry_fix_steps_and_ask_human():
    data = json.loads(to_json([_leak()], Moment.publish))
    finding = data["findings"][0]
    assert isinstance(finding["fix_steps"], list) and finding["fix_steps"]
    assert finding["ask_human"] == []
