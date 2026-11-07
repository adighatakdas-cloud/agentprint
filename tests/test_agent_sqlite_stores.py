"""Goose, Kilo Code and Crush: chat databases read read-only (SQLite), their config and key
stores. agentprint never edits a database: a key there stays with the human."""

import json
import os
from pathlib import Path

import pytest

from agentprint import fix
from agentprint.agents.crush import Crush
from agentprint.agents.goose import Goose
from agentprint.agents.kilo import Kilo
from agentprint.checks import ap008_readable_by_others as ap008
from agentprint.models import Artifact, HookCommand, Kind
from agentprint.scanner import scan
from tests import fakes
from tests.fixtures import agent_homes as homes


def _rel(artifacts, base):
    return {(a.kind, a.path.relative_to(base).as_posix(), a.personal) for a in artifacts}


def _found(findings, agent):
    return {(f.check_id, Path(f.location).name) for f in findings if f.agent == agent}


def _no_ap008(found):
    return {(c, n) for c, n in found if c != "AP008"}


# --- Goose ------------------------------------------------------------------------------------


def test_goose_home_files(tmp_path):
    home = tmp_path / "home"
    homes.goose(home)
    assert _rel(Goose().discover_home(home), home) == {
        (Kind.SETTINGS, ".config/goose/config.yaml", False),
        (Kind.SETTINGS, ".agents/plugins/notify/hooks/hooks.json", False),
        (Kind.HISTORY, ".local/share/goose/sessions/sessions.db", False),
        (Kind.HISTORY, ".local/state/goose/logs/llm_request.0.jsonl", False),
        (Kind.HISTORY, ".config/goose/history.txt", False),
        (Kind.CREDENTIALS, ".config/goose/secrets.yaml", False),
    }


def test_goose_findings(tmp_path):
    home = tmp_path / "home"
    homes.goose(home)
    assert _no_ap008(_found(scan(home, None), "goose")) == {
        ("AP001", "sessions.db"),
        ("AP001", "llm_request.0.jsonl"),
        ("AP004", "hooks.json"),
        ("AP005", "config.yaml"),  # extensions.<name>.envs
    }


def test_goose_extensions_use_cmd_envs_and_uri(tmp_path):
    home = tmp_path / "home"
    homes.goose(home)
    config = Goose().read_config(
        Artifact("goose", Kind.SETTINGS, home / ".config" / "goose" / "config.yaml")
    )
    github, builtin = config.mcp_servers
    assert dict(github.fields)["command"] == "npx"
    assert "env.GITHUB_PERSONAL_ACCESS_TOKEN" in dict(github.fields)
    assert "env_keys" in github.secret_fix
    assert builtin.name == "developer" and builtin.fields == ()
    finding = next(f for f in scan(home, None) if f.check_id == "AP005")
    assert '"GITHUB_PERSONAL_ACCESS_TOKEN" under' in finding.fix
    hooks = Goose().read_config(
        Artifact("goose", Kind.SETTINGS, home / ".agents/plugins/notify/hooks/hooks.json")
    )
    assert hooks.hooks == (HookCommand("Stop", "curl -s https://hooks.example.invalid/x"),)


def test_goose_on_windows(tmp_path):
    home = tmp_path / "win"
    homes.goose_windows(home)
    assert _rel(Goose().discover_home(home), home) == {
        (Kind.HISTORY, "AppData/Roaming/Block/goose/data/sessions/sessions.db", False),
        (Kind.CREDENTIALS, "AppData/Roaming/Block/goose/config/secrets.yaml", False),
    }
    assert _no_ap008(_found(scan(home, None), "goose")) == {("AP001", "sessions.db")}


@pytest.mark.parametrize(
    ("rel", "record"),
    [
        (".local/share/goose/sessions/s.jsonl", True),
        (".local/state/goose/logs/llm_request.1.jsonl", True),
        (".config/goose/history.txt", True),
        ("Library/Application Support/Goose/logs/main.log", True),
        ("AppData/Roaming/Block/goose/data/history.txt", True),
        (".config/goose/config.yaml", False),
        (".config/goose/.goosehints", False),
        (".config/goose/recipes/r.yaml", False),
    ],
)
def test_goose_records(rel, record):
    assert Goose().pure_history(Path("/u") / rel) is record


def test_goose_log_key_is_masked_and_database_key_left_to_the_human(tmp_path):
    home = tmp_path / "home"
    homes.goose(home)
    p = fix.plan(scan(home, None))
    assert {c.path.name for c in p.changes if c.kind == "mask"} == {"llm_request.0.jsonl"}
    assert any("chat database" in m.text and "sessions.db" in m.text for m in p.manual)
    fix.apply(p, home)
    log = home / ".local/state/goose/logs/llm_request.0.jsonl"
    assert fakes.mask_for_test(fakes.STRIPE) in log.read_text()


# --- Kilo Code --------------------------------------------------------------------------------


def test_kilo_home_and_project(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    homes.kilo(home)
    homes.kilo_project(project)
    assert _rel(Kilo().discover_home(home), home) == {
        (Kind.SETTINGS, ".config/kilo/kilo.jsonc", False),
        (Kind.HISTORY, ".local/share/kilo/kilo.db", False),
        (Kind.CREDENTIALS, ".local/share/kilo/auth.json", False),
    }
    assert _rel(Kilo().discover_project(project), project) == {(Kind.SETTINGS, "kilo.jsonc", False)}
    assert _no_ap008(_found(scan(home, project), "kilo")) == {
        ("AP001", "kilo.db"),
        ("AP005", "kilo.jsonc"),  # once for the home file, once for the project's
    }
    assert len([f for f in scan(home, project) if f.check_id == "AP005"]) == 2


def test_kilo_reads_the_mcp_key_and_the_old_wrapper(tmp_path):
    local = {"type": "local", "command": ["npx", "-y", "x", "--token", fakes.GITHUB]}
    path = homes.write(tmp_path / "kilo.json", json.dumps({"mcpServers": {"x": local}}))
    [server] = Kilo().read_config(Artifact("kilo", Kind.SETTINGS, path)).mcp_servers
    assert (server.name, "{env:<VAR>}" in server.secret_fix) == ("x", True)


def test_kilo_old_extension_mcp_file(tmp_path):
    home = tmp_path / "home"
    settings = homes.MAC_CODE / "globalStorage" / "kilocode.kilo-code" / "settings"
    servers = {"gh": homes.stdio("GITHUB_TOKEN", fakes.GITHUB)}
    homes.write(home / settings / "cline_mcp_settings.json", json.dumps({"mcpServers": servers}))
    [finding] = [f for f in scan(home, None) if f.check_id == "AP005"]
    assert finding.agent == "kilo"


def test_kilo_look_alike_database_is_not_read(tmp_path):
    homes.sqlite(tmp_path / "home" / "src" / "kilo.db", "t", [("1", f"k {fakes.AWS}")])
    assert list(Kilo().discover_home(tmp_path / "home")) == []


# --- Crush ------------------------------------------------------------------------------------


def test_crush_home_and_project(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    homes.crush(home)
    homes.crush_project(project)
    assert _rel(Crush().discover_home(home), home) == {
        (Kind.SETTINGS, ".config/crush/crushrc", False),
        (Kind.CREDENTIALS, ".local/share/crush/crush.json", False),
    }
    assert _rel(Crush().discover_project(project), project) == {
        (Kind.HISTORY, ".crush/crush.db", True),
        (Kind.HISTORY, ".crush/logs/crush.log", True),
        (Kind.CREDENTIALS, ".crush/crush.json", True),
        (Kind.SETTINGS, "crush.json", False),
    }
    found = _no_ap008(_found(scan(home, project), "crush"))
    assert found == {
        ("AP001", "crush.db"),
        ("AP001", "crush.log"),
        ("AP002", "crushrc"),  # a literal --api-key
        ("AP004", "crush.json"),
        ("AP005", "crush.json"),
    }


def test_crush_windows_data_file_is_a_key_store(tmp_path):
    home = tmp_path / "win"
    data = {"providers": {"openai": {"api_key": fakes.OPENAI}}}
    homes.write(home / "AppData" / "Local" / "crush" / "crush.json", json.dumps(data))
    assert _rel(Crush().discover_home(home), home) == {
        (Kind.CREDENTIALS, "AppData/Local/crush/crush.json", False)
    }
    assert _no_ap008(_found(scan(home, None), "crush")) == set()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX modes, non-root")
def test_crush_key_store_readable_by_others_is_flagged(tmp_path, monkeypatch):
    top = tmp_path.resolve()
    # Judge folders only up to tmp_path: a test temp dir's real parents are private.
    monkeypatch.setattr(
        ap008, "_parents", lambda p: [f for f in p.parents if top in (f, *f.parents)]
    )
    home = top / "home"
    homes.crush(home)
    store = home / ".local" / "share" / "crush" / "crush.json"
    for folder in (top, home, home / ".local", store.parent.parent, store.parent):
        folder.chmod(0o755)
    store.chmod(0o644)
    # One print per agent folder (~/.local/share/crush, ~/.config/crush).
    exposed = {f.location: f for f in scan(home, None) if f.check_id == "AP008"}
    data = exposed[str(store.parent)]
    assert data.agent == "crush" and "credential store crush.json" in data.detail
    assert data.severity.name == "HIGH"
    assert exposed[str(home / ".config" / "crush")].severity.name == "MEDIUM"  # crushrc


def test_crush_records():
    c = Crush()
    assert c.pure_history(Path("/p/.crush/logs/crush.log"))
    assert c.pure_history(Path("/p/.crush/crush.db"))
    assert not c.pure_history(Path("/p/.crush/crush.json"))
    assert not c.pure_history(Path("/p/CRUSH.md"))
