import json

from rich.console import Console

from agentprint.models import Finding, Severity
from agentprint.report import FailOn, exit_code, print_table, to_json


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
    assert "No findings" in console.export_text()


def test_exit_code_respects_threshold():
    findings = [_finding(Severity.MEDIUM)]
    assert exit_code(findings, FailOn.high) == 0
    assert exit_code(findings, FailOn.medium) == 1
    assert exit_code(findings, FailOn.none) == 0
    assert exit_code([], FailOn.low) == 0
