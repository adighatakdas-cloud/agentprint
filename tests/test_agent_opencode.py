import json

import pytest

from agentprint.agents import ALL_AGENTS
from agentprint.agents.opencode import OpenCode
from agentprint.models import Artifact, Kind
from agentprint.scanner import scan


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix()) for a in artifacts}


def _config(path):
    return OpenCode().read_config(Artifact("opencode", Kind.SETTINGS, path))


def test_opencode_home_files_skip_auth_json(agents_home):
    assert _rel(OpenCode().discover_home(agents_home), agents_home) == {
        (Kind.SETTINGS, ".config/opencode/opencode.json"),
        (Kind.HISTORY, ".local/share/opencode/opencode.db"),
        (Kind.HISTORY, ".local/share/opencode/storage/session_diff/ses_1.json"),
    }


def test_opencode_project_files(agents_project):
    assert _rel(OpenCode().discover_project(agents_project), agents_project) == {
        (Kind.SETTINGS, "opencode.jsonc")
    }


def test_opencode_home_config(agents_home):
    config = _config(agents_home / ".config" / "opencode" / "opencode.json")
    assert [(g.setting, g.value) for g in config.grants] == [('permission.bash["*"]', "allow")]
    assert [s.name for s in config.mcp_servers] == ["tracker", "docs"]
    assert "{env:<VAR>}" in config.mcp_servers[0].secret_fix


def test_opencode_project_jsonc_allows_everything(agents_project):
    [grant] = _config(agents_project / "opencode.jsonc").grants
    assert (grant.setting, grant.value) == ("permission", "allow")


@pytest.mark.parametrize(
    ("permission", "expected"),
    [
        ({"*": "allow"}, ['permission["*"]']),
        ({"bash": "allow"}, ["permission.bash"]),
        ({"bash": {"git *": "allow", "*": "ask"}}, []),
        ({"bash": {"bash *": "allow"}}, ['permission.bash["bash *"]']),
        ({"*": "ask", "bash": "ask", "edit": "allow"}, []),
        ("ask", []),
    ],
)
def test_opencode_permission_shapes(tmp_path, permission, expected):
    f = tmp_path / "opencode.json"
    f.write_text(json.dumps({"permission": permission}))
    assert [g.setting for g in _config(f).grants] == expected


def test_opencode_database_text_is_scanned(agents_home):
    findings = scan(agents_home, None)
    assert any(f.check_id == "AP001" and f.location.endswith("opencode.db") for f in findings), (
        "key in the session database was not found"
    )
    assert not any(f.location.endswith("auth.json") for f in findings)


def test_opencode_package_rules():
    def matches(name):
        return [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(name)]

    assert matches("package/.local/share/opencode/storage/session/x.json") == ["opencode"]
    assert matches("package/opencode.db") == ["opencode"]
    assert matches("package/opencode/auth.json") == ["opencode"]
    assert matches("package/opencode.json") == []
    assert matches("package/.opencode/agent/review.md") == []
    assert matches("package/src/opencode.ts") == []
