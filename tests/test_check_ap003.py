import json

from agentprint.checks import SCAN_CHECKS
from agentprint.checks.ap003_overbroad_permission import CHECK as AP003
from agentprint.checks.base import safe_snippet
from agentprint.models import Artifact, Kind, Severity
from agentprint.scanner import scan
from tests import fakes


def _settings(tmp_path, data, name="settings.json"):
    f = tmp_path / ".claude" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2))
    return Artifact("claude-code", Kind.SETTINGS, f)


def test_ap003_is_registered_with_id_severity_title_and_fix():
    assert AP003 in SCAN_CHECKS
    assert (AP003.id, AP003.severity) == ("AP003", Severity.MEDIUM)
    assert AP003.title and AP003.fix


def test_blanket_bash_rule_is_reported_at_its_line(tmp_path):
    artifact = _settings(tmp_path, {"permissions": {"allow": ["Bash(git status)", "Bash(*)"]}})
    [finding] = AP003.run([artifact])
    assert finding.check_id == "AP003" and finding.severity is Severity.MEDIUM
    assert finding.line == 5
    assert "Bash(*)" in finding.detail and "permissions.allow" in finding.detail
    assert "permissions.allow" in finding.fix


def test_bypass_mode_and_narrow_rules(tmp_path):
    artifact = _settings(
        tmp_path,
        {"permissions": {"allow": ["Bash(npm run test *)"], "defaultMode": "bypassPermissions"}},
    )
    [finding] = AP003.run([artifact])
    assert "bypassPermissions" in finding.detail and '"default"' in finding.fix


def test_broken_settings_give_no_structured_findings_but_secrets_still_found(tmp_path):
    f = tmp_path / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text('{"permissions": {"allow": ["Bash(*)", "Bash(export K=' + fakes.STRIPE)
    assert AP003.run([Artifact("claude-code", Kind.SETTINGS, f)]) == []
    findings = scan(None, tmp_path)
    assert [x.check_id for x in findings] == ["AP002"]


def test_history_files_are_not_parsed_for_permissions(tmp_path):
    artifact = _settings(tmp_path, {"permissions": {"allow": ["Bash(*)"]}})
    assert AP003.run([Artifact("claude-code", Kind.HISTORY, artifact.path)]) == []


def test_every_agent_reports_its_blanket_settings(agents_home, agents_project):
    findings = [f for f in scan(agents_home, agents_project) if f.check_id == "AP003"]
    assert sorted({f.agent for f in findings}) == ["cursor", "gemini-cli", "opencode", "windsurf"]
    assert all(f.fix and f.line for f in findings)


def test_safe_snippet_redacts_strips_and_caps():
    text = f"\x1b[31mcurl -H 'Authorization: Bearer {fakes.OPENAI}'\x07 " + "x" * 300
    out = safe_snippet(text, limit=80)
    assert fakes.OPENAI not in out and "\x1b" not in out and "\x07" not in out
    assert len(out) <= 80 and out.endswith("…")
