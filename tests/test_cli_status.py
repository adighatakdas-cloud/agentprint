import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import agentprint.roster as roster_module
from agentprint import __version__
from agentprint.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _hooked_platform_roster(monkeypatch):
    """Pin the roster where install adds hooks; Windows has its own tests (test_windows_gate)."""
    monkeypatch.setattr(roster_module, "platform", SimpleNamespace(is_windows=lambda: False))


def _home(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    d = tmp_path / ".agentprint"
    d.mkdir()
    (d / "state.json").write_text(
        json.dumps({"version": 1, "files": {"/x": {"agent": "claude-code"}}, "dirs": []})
    )
    return tmp_path


def test_status_json_shape(tmp_path):
    r = runner.invoke(app, ["status", "--home", str(_home(tmp_path)), "--format", "json"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data["tool"] == "agentprint" and data["version"] == __version__
    by = {a["name"]: a for a in data["agents"]}
    assert set(by["cursor"]) == {"name", "label", "found", "armed"}
    assert by["claude-code"]["armed"] is True and by["cursor"]["armed"] is False


def test_status_agent_format_lines(tmp_path):
    r = runner.invoke(app, ["status", "--home", str(_home(tmp_path)), "--format", "agent"])
    lines = r.output.splitlines()
    assert "claude-code armed" in lines
    assert "cursor unarmed" in lines
    assert "codex not found" in lines
    assert lines[-1] == "Arm unarmed agents with: agentprint install"


def test_status_table(tmp_path):
    r = runner.invoke(app, ["status", "--home", str(_home(tmp_path)), "--plain"])
    assert r.exit_code == 0
    assert f"agentprint {__version__} · roster" in r.output
    assert "1 of 2 agents armed · Cursor is not" in r.output
    assert "not found:" in r.output


def test_status_defaults_to_agent_format_inside_an_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    r = runner.invoke(app, ["status", "--home", str(_home(tmp_path))])
    assert "claude-code armed" in r.output.splitlines()


def test_status_exit_zero_without_agents(tmp_path):
    r = runner.invoke(app, ["status", "--home", str(tmp_path), "--format", "agent"])
    assert r.exit_code == 0
    assert "Arm unarmed" not in r.output
    r = runner.invoke(app, ["status", "--home", str(tmp_path), "--plain"])
    assert r.exit_code == 0 and "no AI agents found on this machine" in r.output


def test_status_bad_home(tmp_path):
    assert runner.invoke(app, ["status", "--home", str(tmp_path / "nope")]).exit_code == 2


def test_status_table_columns_align(tmp_path):
    home = _home(tmp_path)
    (home / ".gemini").mkdir()
    (home / ".codex").mkdir()
    r = runner.invoke(app, ["status", "--home", str(home), "--plain"])
    rows = [ln for ln in r.output.splitlines() if ln.rstrip().endswith(("armed", "unarmed"))]
    assert len(rows) >= 3
    cols = {ln.rindex("unarmed" if ln.endswith("unarmed") else "armed") for ln in rows}
    assert len(cols) == 1


def test_status_table_aligns_with_longest_label(tmp_path):
    home = _home(tmp_path)
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    r = runner.invoke(app, ["status", "--home", str(home), "--plain"])
    rows = [ln for ln in r.output.splitlines() if ln.endswith(("armed", "unarmed"))]
    assert any(ln.startswith("Antigravity") for ln in rows)
    assert len({ln.rindex(" ") for ln in rows}) == 1


def test_status_table_wraps_under_its_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "50")
    r = runner.invoke(app, ["status", "--home", str(_home(tmp_path)), "--format", "table"])
    lines = r.output.splitlines()
    assert all(len(ln) <= 50 for ln in lines), r.output
    start = next(i for i, ln in enumerate(lines) if ln.startswith("not found: "))
    assert len(lines) > start + 1
    assert all(ln.startswith(" " * 11) and ln[11] != " " for ln in lines[start + 1 :])


# --- agents a sweep reads in full that install can't arm yet ----------------------------------


def _scanned_home(tmp_path):
    from tests.fixtures import agent_homes as homes

    home = _home(tmp_path)
    homes.cline(home)
    homes.goose(home)
    return home


def test_status_lists_scanned_agents_in_every_format(tmp_path):
    home = _scanned_home(tmp_path)
    data = json.loads(
        runner.invoke(app, ["status", "--home", str(home), "--format", "json"]).output
    )
    by = {a["name"]: a for a in data["scanned"]}
    assert by["cline"] == {"name": "cline", "label": "Cline", "found": True}
    assert by["goose"]["found"] and not by["zed"]["found"]
    assert {"cline", "zed", "qwen-code", "claude-desktop"} <= set(by)
    assert "cline" not in {a["name"] for a in data["agents"]}  # never armed, never "unarmed"

    lines = runner.invoke(app, ["status", "--home", str(home), "--format", "agent"]).output
    lines = lines.splitlines()
    assert "cline scanned (agentprint install can't arm it yet)" in lines
    assert "goose scanned (agentprint install can't arm it yet)" in lines
    assert not any(ln.startswith("zed ") for ln in lines)
    assert lines[-1] == "Arm unarmed agents with: agentprint install"

    table = runner.invoke(app, ["status", "--home", str(home), "--plain"]).output
    assert "1 of 2 agents armed · Cursor is not" in table  # scanned agents are not counted
    rows = [ln for ln in table.splitlines() if ln.startswith(("Cline", "Goose", "Cursor"))]
    assert [ln.split()[-1] for ln in rows] == ["unarmed", "scanned", "scanned"]
    assert len({ln.rindex(" ") for ln in rows}) == 1
    assert "agentprint install can't arm it yet" in table


def test_status_with_only_scanned_agents(tmp_path):
    from tests.fixtures import agent_homes as homes

    homes.zed(tmp_path)
    r = runner.invoke(app, ["status", "--home", str(tmp_path), "--plain"])
    assert "1 agent scanned · none can be armed yet" in r.output
    r = runner.invoke(app, ["status", "--home", str(tmp_path), "--format", "agent"])
    assert "zed scanned (agentprint install can't arm it yet)" in r.output.splitlines()
    assert "Arm unarmed" not in r.output


def test_scanned_survives_a_broken_agent_module(tmp_path, monkeypatch):
    from agentprint.agents.zed import Zed
    from agentprint.roster import scanned

    def boom(self, home):
        raise RuntimeError("bug")

    monkeypatch.setattr(Zed, "discover_home", boom)
    by = {s.name: s for s in scanned(tmp_path)}
    assert by["zed"].found is False and by["zed"].armed is False
