from collections import Counter

from agentprint.checks.ap001_history_secret import CHECK as AP001
from agentprint.checks.ap002_settings_secret import CHECK as AP002
from agentprint.models import Artifact, Kind, Severity
from agentprint.scanner import scan
from tests import fakes


def test_scan_finds_every_planted_key(fake_home, fake_project):
    findings = scan(fake_home, fake_project)
    # The Codex config.toml key sits in an MCP server entry: AP005, not AP002.
    assert Counter(f.check_id for f in findings) == {"AP001": 5, "AP002": 2, "AP005": 1}
    assert findings[0].severity is Severity.CRITICAL  # most severe first


def test_auth_json_is_not_reported(fake_home):
    assert not any("auth.json" in f.location for f in scan(fake_home, None))


def test_findings_are_redacted(fake_home, fake_project):
    blob = repr(scan(fake_home, fake_project))
    for key in fakes.ALL.values():
        assert key not in blob


def test_finding_points_at_file_and_line(tmp_path):
    f = tmp_path / "history.jsonl"
    f.write_text("hello\n" + f"key {fakes.GITHUB}\n")
    [finding] = AP001.run([Artifact("claude-code", Kind.HISTORY, f)])
    assert (finding.location, finding.line) == (str(f), 2)
    assert finding.detail == f"GitHub token: {fakes.GITHUB[:4]}…(len {len(fakes.GITHUB)})"


def test_repeated_key_is_one_finding_with_count(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(f"{fakes.ANTHROPIC}\n" * 300)
    [finding] = AP001.run([Artifact("claude-code", Kind.HISTORY, f)])
    assert finding.count == 300


def test_broken_json_settings_still_scanned(tmp_path):
    f = tmp_path / "settings.local.json"
    f.write_text('{"permissions": {"allow": ["Bash(export K=' + fakes.STRIPE)  # truncated
    [finding] = AP002.run([Artifact("claude-code", Kind.SETTINGS, f)])
    assert finding.check_id == "AP002"


def test_checks_only_read_their_own_kind(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(fakes.AWS)
    assert AP002.run([Artifact("codex", Kind.HISTORY, f)]) == []
