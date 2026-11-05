"""Layout A, "Grouped": the scan and pack results a person sees (AI agents get --format agent)."""

from pathlib import Path

from rich.console import Console
from rich.text import Text

from agentprint.models import Finding, Severity
from agentprint.report import FailOn
from agentprint.secrets import redact
from agentprint.ui.results import render
from agentprint.ui.theme import console as themed_console
from tests import fakes

HEADER = "agentprint scan  ~ + ./my-app · roster 4 · 0.41s"


def _finding(check_id, sev, location, line, detail, fix, count=1):
    return Finding(check_id, sev, "title", "claude-code", location, line, detail, fix, count)


def _three() -> list[Finding]:
    home = Path.home()
    return [  # deliberately not in severity order: render groups them
        _finding(
            "AP001",
            Severity.HIGH,
            str(home / ".claude" / "history.jsonl"),
            1,
            f"Anthropic API key: {redact(fakes.ANTHROPIC)}",
            "Replace the key: it was saved in plain text.",
            count=2,
        ),
        _finding(
            "AP005",
            Severity.CRITICAL,
            str(home / ".claude.json"),
            None,
            f"GitHub token: {redact(fakes.GITHUB)} in MCP server 'github'",
            "Rotate the GitHub token, then pass it in through an environment variable.",
        ),
        _finding(
            "AP006",
            Severity.CRITICAL,
            "my-app/.claude/settings.local.json",
            None,
            "would ship in your npm package",
            "Add .claude/ to .npmignore, then preflight again.",
        ),
    ]


def _text(findings, fail_on=FailOn.high, header=None, width=120) -> str:
    out = Console(record=True, width=width)
    render(findings, out, fail_on=fail_on, header=header)
    return out.export_text()


EXPECTED = """\
agentprint scan  ~ + ./my-app · roster 4 · 0.41s

critical  2
  AP005  ~/.claude.json
         GitHub token: ghp_…(len 40) masked in MCP server 'github'
  AP006  my-app/.claude/settings.local.json
         would ship in your npm package

high      1
  AP001  ~/.claude/history.jsonl:1
         Anthropic API key: sk-a…(len 105) masked (×2)

Fix
  1  AP005  Rotate the GitHub token, then pass it in through an environment variable.
  2  AP006  Add .claude/ to .npmignore, then preflight again.
  3  AP001  Replace the key: it was saved in plain text.

 BLOCK   3 prints · 2 critical · 1 high · keys masked
"""


def test_grouped_layout_text():
    assert _text(_three(), header=HEADER) == EXPECTED


def test_clear_when_no_findings():
    text = _text([], header=HEADER)
    assert " CLEAR " in text and "no prints · keys masked" in text
    assert "BLOCK" not in text and "Fix" not in text


def test_below_threshold_is_clear_with_count():
    low = [f for f in _three() if f.severity is Severity.HIGH]
    text = _text(low, fail_on=FailOn.critical)
    assert text.splitlines()[-1] == " CLEAR   1 print below --fail-on critical · keys masked"
    assert "AP001" in text  # the prints are still listed, with their fix


def test_block_counts_only_nonzero_severities():
    text = _text(_three(), fail_on=FailOn.critical)
    assert text.splitlines()[-1] == " BLOCK   3 prints · 2 critical · 1 high · keys masked"


def test_fix_list_is_unique_and_complete():
    same = _three()[1]
    twice = [same, _finding("AP005", Severity.CRITICAL, "/b", 2, "d", same.fix)]
    lines = _text(twice).splitlines()
    fix_lines = lines[lines.index("Fix") + 1 :]
    assert [ln for ln in fix_lines if ln.startswith("  1  ") or ln.startswith("  2  ")] == [
        f"  1  AP005  {same.fix}"
    ]


def test_markup_in_scanned_text_is_literal():
    f = _finding("AP001", Severity.HIGH, "/x/[bold]p[/]", 1, "[red]x[/red] [/]", "Fix [it].")
    text = _text([f])
    assert "[red]x[/red] [/]" in text
    assert "/x/[bold]p[/]:1" in text and "Fix [it]." in text


def test_escape_codes_in_scanned_text_are_removed():
    f = _finding("AP001", Severity.HIGH, "/x/\x1b[31mred\x1b[0m\n/f", 1, "\x1b]0;pwned\x07d", "x")
    out = themed_console({}, record=True, force_terminal=True, width=120)
    render([f], out)
    text = out.export_text(styles=False)
    assert "\x1b" not in text and "[31m" not in text and "pwned" not in text


def test_narrow_console_does_not_raise():
    f = _finding("AP003", Severity.MEDIUM, "/" + "d" * 119, 3, "Bash(*) lets it run anything", "x")
    text = _text([f], header=HEADER, width=40)
    assert "BLOCK" not in text and "CLEAR" in text


def test_never_prints_full_key():
    findings = [
        _finding("AP001", Severity.HIGH, f"/h/{name}.jsonl", 1, f"{name}: {redact(key)}", "x")
        for name, key in fakes.ALL.items()
    ]
    text = _text(findings)
    for key in fakes.ALL.values():
        assert key not in text
        assert f"{redact(key)} masked" in text


class _Lines:
    """Stands in for a Console and keeps every printed line as rich Text, styles by name."""

    def __init__(self) -> None:
        self.lines: list[Text] = []

    def print(self, line: Text | None = None) -> None:
        self.lines.append(line or Text())


def _styled(findings, header=None, fail_on=FailOn.high) -> list[tuple[str, str]]:
    out = _Lines()
    render(findings, out, fail_on=fail_on, header=header)  # type: ignore[arg-type]
    pairs = []
    for line in out.lines:
        if line.style:
            pairs.append((line.plain, str(line.style)))
        pairs += [(line.plain[s.start : s.end], str(s.style)) for s in line.spans]
    return pairs


def test_stub_is_amber_and_masked_is_dim():
    pairs = _styled(_three())
    assert ("sk-a…(len 105)", "ap.amber") in pairs and ("ghp_…(len 40)", "ap.amber") in pairs
    assert (" masked", "ap.dim") in pairs and (" (×2)", "ap.dim") in pairs


def test_layout_styles():
    pairs = _styled(_three(), header=HEADER)
    assert (HEADER, "ap.dim") in pairs
    assert ("critical  ", "ap.sev.critical") in pairs and ("2", "ap.dim") in pairs
    assert ("high      ", "ap.sev.high") in pairs
    assert ("AP005", "ap.dim") in pairs and ("~/.claude.json", "ap.strong") in pairs
    assert ("Fix", "ap.dim") in pairs and ("  1  AP005", "ap.dim") in pairs
    assert (" BLOCK ", "ap.block") in pairs
    styled_text = {text for text, _ in pairs}
    assert "would ship in your npm package" not in styled_text  # detail text stays normal
    assert not any("Rotate the GitHub token" in text for text in styled_text)


def test_clear_label_style():
    assert (" CLEAR ", "ap.clear_label") in _styled([])
    assert (" CLEAR ", "ap.clear_label") in _styled(_three(), fail_on=FailOn.none)


def test_themed_console_renders_without_error():
    out = themed_console({}, record=True, force_terminal=True, width=120)
    render(_three(), out, header=HEADER)
    assert out.export_text(styles=False) == EXPECTED
