"""Cline: home folder (~/.cline/data and the VS Code extension's folder), read in full."""

import json
from pathlib import Path

from agentprint import fix
from agentprint.agents import ALL_AGENTS
from agentprint.agents.cline import Cline
from agentprint.models import Artifact, Kind
from agentprint.scanner import scan
from tests import fakes
from tests.fixtures import agent_homes as homes

EXT = homes.MAC_CODE / "globalStorage" / "saoudrizwan.claude-dev"


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix()) for a in artifacts}


def _home(tmp_path):
    home = tmp_path / "home"
    homes.cline(home)
    return home


def test_cline_home_files(tmp_path):
    home = _home(tmp_path)
    assert _rel(Cline().discover_home(home), home) == {
        (Kind.SETTINGS, ".cline/data/settings/cline_mcp_settings.json"),
        (Kind.SETTINGS, ".cline/data/settings/global-settings.json"),
        (Kind.SETTINGS, ".cline/data/globalState.json"),
        (Kind.HISTORY, ".cline/data/sessions/s-0001/messages.jsonl"),
        (Kind.HISTORY, ".cline/data/db/sessions.db"),
        (Kind.HISTORY, ".cline/data/logs/cline.log"),
        (Kind.HISTORY, f"{EXT.as_posix()}/tasks/t1/api_conversation_history.json"),
        (Kind.CREDENTIALS, ".cline/data/secrets.json"),
        (Kind.CREDENTIALS, ".cline/data/settings/providers.json"),
    }


def test_cline_extension_folder_on_linux_and_windows(tmp_path):
    for user in (Path(".config") / "Code" / "User", homes.WIN_APPDATA / "Code" / "User"):
        home = tmp_path / user.parts[0]
        ext = home / user / "globalStorage" / "saoudrizwan.claude-dev"
        homes.write(ext / "tasks" / "t1" / "ui_messages.json", "[]")
        homes.write(ext / "settings" / "cline_mcp_settings.json", "{}")
        assert _rel(Cline().discover_home(home), home) == {
            (
                Kind.HISTORY,
                f"{user.as_posix()}/globalStorage/saoudrizwan.claude-dev/tasks/t1/ui_messages.json",
            ),
            (
                Kind.SETTINGS,
                f"{user.as_posix()}/globalStorage/saoudrizwan.claude-dev/settings/"
                "cline_mcp_settings.json",
            ),
        }


def test_cline_keys_found_and_key_stores_not_read(tmp_path):
    home = _home(tmp_path)
    found = {(f.check_id, Path(f.location).name) for f in scan(home, None) if f.agent == "cline"}
    assert {f for f in found if f[0] != "AP008"} == {
        ("AP001", "messages.jsonl"),
        ("AP001", "sessions.db"),
        ("AP001", "api_conversation_history.json"),
        ("AP005", "cline_mcp_settings.json"),
    }
    assert not {"secrets.json", "providers.json"} & {
        name for check, name in found if check != "AP008"
    }


def test_cline_mcp_servers_and_secret_fix(tmp_path):
    home = _home(tmp_path)
    path = home / ".cline" / "data" / "settings" / "cline_mcp_settings.json"
    [server] = Cline().read_config(Artifact("cline", Kind.SETTINGS, path)).mcp_servers
    assert server.name == "github"
    assert "chmod 600" in server.secret_fix and "no environment-variable" in server.secret_fix
    other = home / ".cline" / "data" / "globalState.json"
    assert Cline().read_config(Artifact("cline", Kind.SETTINGS, other)).mcp_servers == ()


def test_cline_records_are_masked_and_settings_are_not(tmp_path):
    cline = Cline()
    data = Path("/u/.cline/data")
    for record in ("sessions/s1/m.jsonl", "logs/cline.log", "tasks/t/a.json", "db/sessions.db"):
        assert cline.pure_history(data / record), record
    assert cline.pure_history(Path("/u") / EXT / "tasks" / "t1" / "ui_messages.json")
    assert not cline.pure_history(data / "settings" / "cline_mcp_settings.json")
    assert not cline.pure_history(Path("/u/.cline/rules/style.md"))

    home = _home(tmp_path)
    p = fix.plan(scan(home, None))
    masked = {c.path.name for c in p.changes if c.kind == "mask"}
    assert masked == {"messages.jsonl", "api_conversation_history.json"}  # never the database
    fix.apply(p, home)
    history = json.loads(
        (home / EXT / "tasks" / "t1" / "api_conversation_history.json").read_text()
    )
    assert fakes.STRIPE not in json.dumps(history)
    assert fakes.mask_for_test(fakes.STRIPE) in json.dumps(history)


def test_cline_package_rule_unchanged():
    def matches(name):
        return [r.agent for a in ALL_AGENTS for r in a.package_rules if r.pattern.search(name)]

    assert matches("package/.cline/data/secrets.json") == ["cline"]
    assert matches("x/globalStorage/saoudrizwan.claude-dev/tasks/t/a.json") == ["cline"]
    assert matches("package/.clinerules/style.md") == []
    assert matches("package/.cline/rules/style.md") == []


def test_cline_extension_state_is_only_the_task_list(tmp_path):
    home = tmp_path / "home"
    state = home / EXT / "state"
    homes.write(state / "taskHistory.json", "[]")
    homes.write(state / "other.json", "{}")  # not documented: not read
    assert _rel(Cline().discover_home(home), home) == {
        (Kind.HISTORY, f"{EXT.as_posix()}/state/taskHistory.json")
    }
    assert Cline().pure_history(state / "taskHistory.json")
    assert not Cline().pure_history(state / "other.json")
