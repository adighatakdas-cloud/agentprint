"""Zed (threads database, settings.json context_servers) and Aider (home config and key files;
a .env counts as Aider's only next to Aider's own files)."""

from pathlib import Path

import pytest

from agentprint import fix
from agentprint.agents.aider import Aider
from agentprint.agents.zed import Zed
from agentprint.models import Artifact, Kind
from agentprint.scanner import scan
from tests import fakes
from tests.fixtures import agent_homes as homes


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix(), a.personal) for a in artifacts}


def _found(findings, agent):
    return {(f.check_id, Path(f.location).name) for f in findings if f.agent == agent}


def _no_ap008(found):
    return {(c, n) for c, n in found if c != "AP008"}


# --- Zed --------------------------------------------------------------------------------------


def test_zed_home_and_project(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    homes.zed(home)
    homes.zed_project(project)
    assert _rel(Zed().discover_home(home), home) == {
        (Kind.SETTINGS, ".config/zed/settings.json", False),
        (Kind.HISTORY, "Library/Application Support/Zed/threads/threads.db", False),
    }
    # Shared editor config: read for MCP keys, never personal (so never an AP007 print).
    assert _rel(Zed().discover_project(project), project) == {
        (Kind.SETTINGS, ".zed/settings.json", False)
    }
    assert _no_ap008(_found(scan(home, project), "zed")) == {
        ("AP001", "threads.db"),
        ("AP005", "settings.json"),
        ("AP011", "settings.json"),  # npx -y with no version
    }
    assert len([f for f in scan(home, project) if f.check_id == "AP005"]) == 2


def test_zed_jsonc_settings_and_secret_fix(tmp_path):
    home = tmp_path / "home"
    homes.zed(home)
    path = home / ".config" / "zed" / "settings.json"
    [server] = Zed().read_config(Artifact("zed", Kind.SETTINGS, path)).mcp_servers
    assert server.name == "api" and "OAuth" in server.secret_fix


@pytest.mark.parametrize(
    "data", [Path(".local") / "share" / "zed", Path("AppData") / "Local" / "Zed"]
)
def test_zed_threads_on_linux_and_windows(tmp_path, data):
    home = tmp_path / "home"
    homes.sqlite(home / data / "threads" / "threads.db", "threads", [("t1", "hello")])
    homes.write(home / "AppData" / "Roaming" / "Zed" / "settings.json", "{}")
    kinds = {(a.kind, a.path.name) for a in Zed().discover_home(home)}
    assert kinds == {(Kind.HISTORY, "threads.db"), (Kind.SETTINGS, "settings.json")}


def test_zed_database_key_is_left_to_the_human(tmp_path):
    home = tmp_path / "home"
    homes.zed(home)
    p = fix.plan([f for f in scan(home, None) if f.check_id == "AP001"])
    assert p.changes == [] and any("chat database" in m.text for m in p.manual)


# --- Aider ------------------------------------------------------------------------------------


def test_aider_home_files(tmp_path):
    home = tmp_path / "home"
    homes.aider(home)
    assert _rel(Aider().discover_home(home), home) == {
        (Kind.SETTINGS, ".aider.conf.yml", False),
        (Kind.HISTORY, ".aider.chat.history.md", False),
        (Kind.CREDENTIALS, ".aider/oauth-keys.env", False),
        (Kind.CREDENTIALS, ".env", False),
    }
    assert _no_ap008(_found(scan(home, None), "aider")) == {
        ("AP001", ".aider.chat.history.md"),
        ("AP002", ".aider.conf.yml"),  # openai-api-key written in the config
    }


def test_aider_project_env_is_personal_only_where_aider_ran(tmp_path):
    project = tmp_path / "proj"
    homes.aider_project(project)
    assert _rel(Aider().discover_project(project), project) == {
        (Kind.HISTORY, ".aider.chat.history.md", True),
        (Kind.HISTORY, ".aider.input.history", True),
        (Kind.CREDENTIALS, ".env", True),
    }
    other = tmp_path / "other"
    homes.write(other / ".env", f"OPENAI_API_KEY={fakes.OPENAI}\n")
    homes.write(other / "docs" / "aider.md", "We tried Aider once.\n")
    assert list(Aider().discover_project(other)) == []
    home = tmp_path / "plain-home"
    homes.write(home / ".env", "X=1\n")
    assert list(Aider().discover_home(home)) == []


def test_aider_transcript_key_is_masked(tmp_path):
    home = tmp_path / "home"
    homes.aider(home)
    p = fix.plan(scan(home, None))
    assert [c.path.name for c in p.changes if c.kind == "mask"] == [".aider.chat.history.md"]
    fix.apply(p, home)
    text = (home / ".aider.chat.history.md").read_text()
    assert fakes.GITHUB not in text and fakes.mask_for_test(fakes.GITHUB) in text
    assert fakes.OPENAI in (home / ".aider.conf.yml").read_text()  # config: the human's


def test_aider_has_no_config_to_read(tmp_path):
    homes.aider(tmp_path)
    artifact = Artifact("aider", Kind.SETTINGS, tmp_path / ".aider.conf.yml")
    assert Aider().read_config(artifact).mcp_servers == ()
