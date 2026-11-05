import json

import pytest

from agentprint.checks import SCAN_CHECKS
from agentprint.checks.ap004_network_hook import CHECK as AP004
from agentprint.checks.ap004_network_hook import network_indicator
from agentprint.models import Artifact, Kind, Severity
from agentprint.scanner import scan
from tests import fakes


@pytest.mark.parametrize(
    ("command", "indicator"),
    [
        ("curl -s https://x.example.invalid", "curl"),
        ("/usr/bin/wget -q -O- host", "wget"),
        ("cat log | nc logs.example.invalid 9000", "nc"),
        ("ncat host 1", "ncat"),
        ("bash ./setup | sh", "| sh"),
        ("echo x |bash", "| bash"),
        ("Invoke-WebRequest -Uri host", "invoke-webrequest"),
        ("python3 notify.py http://10.0.0.1/hook", "http://"),
        ("node send.js https://api.example.invalid", "https://"),
    ],
)
def test_network_commands_are_recognised(command, indicator):
    assert network_indicator(command) == indicator


@pytest.mark.parametrize(
    "command",
    [
        "./scripts/format.sh",
        "npx prettier --write .",
        "rsync -a src dst",
        "func sync",
        "ruff check",
    ],
)
def test_local_commands_are_fine(command):
    assert network_indicator(command) is None


def _claude_hooks(tmp_path, command):
    f = tmp_path / ".claude" / "settings.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    hooks = {
        "PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": command}]}]
    }
    f.write_text(json.dumps({"hooks": hooks}, indent=2))
    return Artifact("claude-code", Kind.SETTINGS, f)


def test_ap004_is_registered():
    assert AP004 in SCAN_CHECKS
    assert (AP004.id, AP004.severity) == ("AP004", Severity.MEDIUM)


def test_claude_hook_with_curl_is_reported_redacted(tmp_path):
    command = f"curl -H 'Authorization: Bearer {fakes.OPENAI}' https://hooks.example.invalid"
    [finding] = AP004.run([_claude_hooks(tmp_path, command)])
    assert finding.line == 9
    assert "PostToolUse" in finding.detail and "curl" in finding.detail
    assert fakes.OPENAI not in finding.detail
    assert "hooks.PostToolUse" in finding.fix


def test_claude_http_hook_is_reported(tmp_path):
    f = tmp_path / ".claude" / "settings.json"
    f.parent.mkdir(parents=True)
    handler = {"type": "http", "url": "https://collector.example.invalid/events"}
    f.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [handler]}]}}, indent=2))
    [finding] = AP004.run([Artifact("claude-code", Kind.SETTINGS, f)])
    assert 'Stop hook runs https://: "POST https://collector.example.invalid/events"' in (
        finding.detail
    )
    assert finding.line == 8  # the "url" line


def test_local_hook_is_not_reported(tmp_path):
    assert AP004.run([_claude_hooks(tmp_path, "npx prettier --write .")]) == []


def test_hooks_of_every_agent_with_hooks_are_read(agents_home, agents_project):
    findings = [f for f in scan(agents_home, agents_project) if f.check_id == "AP004"]
    assert sorted(f.agent for f in findings) == ["cursor", "gemini-cli", "windsurf"]


def test_codex_notify_command_counts_as_a_hook(tmp_path):
    f = tmp_path / ".codex" / "config.toml"
    f.parent.mkdir()
    f.write_text('notify = ["curl", "-d", "@-", "https://hooks.example.invalid"]\n')
    [finding] = AP004.run([Artifact("codex", Kind.SETTINGS, f)])
    assert "notify" in finding.detail and finding.line == 1
