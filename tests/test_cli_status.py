import json

from typer.testing import CliRunner

from agentprint import __version__
from agentprint.cli import app

runner = CliRunner()


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
