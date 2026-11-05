import json
from collections import Counter

from agentprint.checks import SCAN_CHECKS
from agentprint.checks.ap005_mcp_inline_secret import CHECK as AP005
from agentprint.models import Artifact, Kind, Severity
from agentprint.scanner import scan
from agentprint.secrets import redact
from tests import fakes


def test_ap005_is_registered():
    assert AP005 in SCAN_CHECKS
    assert (AP005.id, AP005.severity) == ("AP005", Severity.HIGH)


def test_cursor_env_key_is_reported_with_line_and_concrete_fix(agents_home):
    f = agents_home / ".cursor" / "mcp.json"
    [finding] = AP005.run([Artifact("cursor", Kind.SETTINGS, f)])
    assert finding.line == 1
    assert finding.detail == (
        f"GitHub token: {redact(fakes.GITHUB)} in MCP server 'github' "
        "(env.GITHUB_PERSONAL_ACCESS_TOKEN)"
    )
    assert "${env:GITHUB_PERSONAL_ACCESS_TOKEN}" in finding.fix
    assert finding.fix.startswith("Ask the human to rotate")


def test_every_place_in_every_agent_is_covered(agents_home, agents_project):
    findings = [f for f in scan(agents_home, agents_project) if f.check_id == "AP005"]
    assert Counter(f.agent for f in findings) == {
        "cursor": 2,  # home env, project headers
        "gemini-cli": 2,  # home env, project headers
        "windsurf": 1,  # headers
        "opencode": 1,  # --api-key in the command list
    }
    assert all(fakes.FAKE * 3 not in f.detail for f in findings)


def test_mcp_secret_is_not_also_reported_as_ap002(agents_home, agents_project, fake_home):
    findings = scan(fake_home, agents_project) + scan(agents_home, None)
    ap005 = {f.location for f in findings if f.check_id == "AP005"}
    assert ap005  # codex config.toml, cursor/gemini/windsurf/opencode configs
    assert not [f for f in findings if f.check_id == "AP002" and f.location in ap005]


def test_other_secrets_in_a_mixed_file_stay_ap002(tmp_path):
    data = {
        "mcpServers": {"pay": {"command": "npx", "env": {"STRIPE_KEY": fakes.STRIPE}}},
        "notes": f"old key {fakes.AWS}",
    }
    (tmp_path / ".claude.json").write_text(json.dumps(data, indent=2))
    findings = scan(tmp_path, None)
    assert sorted((f.check_id, f.detail.split(":")[0]) for f in findings) == [
        ("AP002", "AWS access key ID"),
        ("AP005", "Stripe live key"),
    ]


def test_env_references_are_not_secrets(tmp_path):
    f = tmp_path / "mcp.json"
    servers = {"a": {"env": {"API_KEY": "${env:API_KEY}"}, "headers": {"X": "Bearer ${TOKEN}"}}}
    f.write_text(json.dumps({"mcpServers": servers}))
    assert AP005.run([Artifact("cursor", Kind.SETTINGS, f)]) == []
