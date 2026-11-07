"""scan and pack pick their output for whoever runs them: an AI agent gets --format agent,
a person gets the grouped layout, and pipes and --plain get it without escape codes."""

import io
import json
import re
import tarfile

import pytest
from rich.console import Console
from typer.testing import CliRunner

from agentprint.cli import app
from agentprint.report import _short
from tests import fakes


@pytest.fixture(autouse=True)
def _no_forced_color(monkeypatch):
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE", "NO_COLOR", "CI", "COLORTERM"):
        monkeypatch.delenv(name, raising=False)
    # The same terminal everywhere: GitHub Actions has no TERM, and rich then picks 16 colors.
    monkeypatch.setenv("TERM", "xterm-256color")


SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _color_codes(text: str) -> list[str]:
    """Every SGR escape that sets a color: 30-49 and 90-107 (incl. 38/48 for 256 and true
    color). Bold (1) and reset (0) are not colors."""
    found = []
    for params in SGR.findall(text):
        numbers = [int(p) for p in params.split(";") if p]
        if any(30 <= n <= 49 or 90 <= n <= 107 for n in numbers):
            found.append(params)
    return found


def _scan(fake_home, fake_project, *extra, env=None):
    args = ["scan", str(fake_project), "--home", str(fake_home), *extra]
    return CliRunner(env=env).invoke(app, args)


def _tarball(tmp_path):
    data = b'{"x": 1}'
    tgz = tmp_path / "p.tgz"
    with tarfile.open(tgz, "w:gz") as tf:
        info = tarfile.TarInfo("package/.claude/settings.local.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return tgz


def test_agent_marker_switches_scan_to_agent_format(fake_home, fake_project):
    result = _scan(fake_home, fake_project, env={"CLAUDECODE": "1"})
    assert result.exit_code == 1
    assert result.output.splitlines()[0].startswith("agentprint: ")
    assert "\x1b[" not in result.output and "keys masked" not in result.output


def test_agent_marker_switches_pack_to_agent_format(tmp_path):
    result = CliRunner(env={"GEMINI_CLI": "1"}).invoke(app, ["pack", str(_tarball(tmp_path))])
    assert result.exit_code == 1
    assert result.output.startswith("agentprint: BLOCK")


def test_explicit_format_wins_over_marker(fake_home, fake_project):
    result = _scan(fake_home, fake_project, "--format", "json", env={"CLAUDECODE": "1"})
    assert json.loads(result.output)["tool"] == "agentprint"
    result = _scan(fake_home, fake_project, "--format", "table", env={"CLAUDECODE": "1"})
    assert "BLOCK" in result.output and not result.output.startswith("agentprint: ")


def test_piped_human_output_has_no_ansi(fake_home, fake_project):
    result = _scan(fake_home, fake_project)
    assert result.exit_code == 1
    assert "\x1b[" not in result.output
    assert "BLOCK" in result.output and "keys masked" in result.output
    for key in fakes.ALL.values():
        assert key not in result.output


def test_plain_flag_has_no_ansi(fake_home, fake_project):
    forced = {"FORCE_COLOR": "1"}  # rich would color this run, as in a real terminal
    assert "\x1b[" in _scan(fake_home, fake_project, env=forced).output
    result = _scan(fake_home, fake_project, "--plain", env=forced)
    assert result.exit_code == 1
    assert "\x1b[" not in result.output and "BLOCK" in result.output


def test_pack_plain_flag_has_no_ansi(tmp_path):
    runner = CliRunner(env={"FORCE_COLOR": "1"})
    result = runner.invoke(app, ["pack", str(_tarball(tmp_path)), "--plain"])
    assert result.exit_code == 1
    assert "\x1b[" not in result.output and "BLOCK" in result.output


def test_color_code_check_sees_every_color_system():
    assert _color_codes("\x1b[1;93mx\x1b[0m \x1b[37my\x1b[0m") == ["1;93", "37"]  # 16 colors
    assert _color_codes("\x1b[38;5;203mx \x1b[1;38;2;255;176;32my") == [
        "38;5;203",
        "1;38;2;255;176;32",
    ]
    assert _color_codes("\x1b[1mbold\x1b[0m \x1b[7mreverse\x1b[0m") == []


@pytest.mark.parametrize("term", ["xterm-256color", None])
def test_no_color_env_is_honoured(fake_home, fake_project, monkeypatch, term):
    if term is None:
        monkeypatch.delenv("TERM")  # as on GitHub Actions: rich falls back to 16 colors
    forced = _scan(fake_home, fake_project, env={"FORCE_COLOR": "1"})
    assert _color_codes(forced.output)  # the control: forced color is on
    result = _scan(fake_home, fake_project, env={"FORCE_COLOR": "1", "NO_COLOR": "1"})
    assert "BLOCK" in result.output
    assert _color_codes(result.output) == []  # bold may stay; no color


def test_scan_header_names_home_project_roster_and_time(fake_home, fake_project):
    first = _scan(fake_home, fake_project).output.splitlines()[0]
    home, project = re.escape(_short(str(fake_home))), re.escape(str(fake_project))
    pattern = rf"agentprint scan  {home} \+ {project}"
    assert re.fullmatch(pattern + r" · roster \d+ · \d+\.\d\ds", first), first


def test_scan_header_without_home(tmp_path):
    first = CliRunner().invoke(app, ["scan", str(tmp_path), "--no-home"]).output.splitlines()[0]
    assert re.fullmatch(rf"agentprint scan  {re.escape(str(tmp_path))} · \d+\.\d\ds", first)


def test_pack_header_names_the_package(tmp_path):
    first = CliRunner().invoke(app, ["pack", str(_tarball(tmp_path))]).output.splitlines()[0]
    assert re.fullmatch(r"agentprint pack  p\.tgz · \d+\.\d\ds", first), first


def test_no_spinner_when_piped(fake_home, fake_project):
    assert "sweeping" not in _scan(fake_home, fake_project).output


@pytest.fixture
def spinner_calls(monkeypatch):
    """Pretend should_animate() says yes (unless --plain), and record every spinner started."""
    calls = []
    real = Console.status

    def status(self, *args, **kwargs):
        calls.append((args, kwargs))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Console, "status", status)
    monkeypatch.setattr("agentprint.cli.should_animate", lambda env, out, plain=False: not plain)
    return calls


def test_spinner_runs_while_a_person_waits(fake_home, fake_project, tmp_path, spinner_calls):
    _scan(fake_home, fake_project)
    CliRunner().invoke(app, ["pack", str(_tarball(tmp_path))])
    style = {"spinner": "dots", "spinner_style": "ap.dim"}
    assert spinner_calls == [(("sweeping…",), style), (("preflighting…",), style)]


def test_spinner_never_on_the_ai_path_or_with_plain(fake_home, fake_project, spinner_calls):
    _scan(fake_home, fake_project, env={"CLAUDECODE": "1"})
    for fmt in ("agent", "json", "sarif"):
        _scan(fake_home, fake_project, "--format", fmt)
    _scan(fake_home, fake_project, "--plain")
    assert spinner_calls == []


AGENT_FORCED = {"CLAUDECODE": "1", "FORCE_COLOR": "1", "TTY_COMPATIBLE": "1"}


def test_agent_marker_means_no_color_in_the_table_layout(fake_home, fake_project):
    """Any marker: no color, even when an AI agent asks for the person's layout."""
    result = _scan(fake_home, fake_project, "--format", "table", env=AGENT_FORCED)
    assert "BLOCK" in result.output and "\x1b[" not in result.output


def test_agent_marker_means_no_color_in_status(tmp_path):
    (tmp_path / ".claude").mkdir()
    args = ["status", "--home", str(tmp_path), "--format", "table"]
    result = CliRunner(env=AGENT_FORCED).invoke(app, args)
    assert result.exit_code == 0 and "agent armed" in result.output
    assert "\x1b[" not in result.output


def test_agent_marker_welcome_is_plain_and_not_remembered(tmp_path):
    from agentprint.ui.welcome import seen_welcome

    result = CliRunner(env=AGENT_FORCED).invoke(app, ["welcome", "--home", str(tmp_path)])
    assert result.exit_code == 0 and "The security check" in result.output
    assert "\x1b[" not in result.output
    assert not seen_welcome(tmp_path)  # an AI agent is not a person seeing the welcome


def _top_plain_cases(fake_home, fake_project, tmp_path):
    return {
        "scan": ["scan", str(fake_project), "--home", str(fake_home)],
        "pack": ["pack", str(_tarball(tmp_path))],
        "status": ["status", "--home", str(fake_home), "--format", "table"],
        "welcome": ["welcome", "--home", str(fake_home)],
    }


@pytest.mark.parametrize("command", ["scan", "pack", "status", "welcome"])
def test_top_level_plain_reaches_the_command(fake_home, fake_project, tmp_path, command):
    """`agentprint --plain scan` is as plain as `agentprint scan --plain`."""
    args = _top_plain_cases(fake_home, fake_project, tmp_path)[command]
    forced = CliRunner(env={"FORCE_COLOR": "1"})
    assert "\x1b[" in forced.invoke(app, args).output  # the control: color is forced on
    result = forced.invoke(app, ["--plain", *args])
    assert result.output and "\x1b[" not in result.output


def test_top_level_plain_makes_command_help_plain(monkeypatch):
    import typer.rich_utils

    from agentprint import cli

    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setattr(cli, "plain_help", lambda *a: False)  # a person at a real terminal
    assert "\x1b[" in CliRunner().invoke(app, ["scan", "--help"]).output  # the control
    result = CliRunner().invoke(app, ["--plain", "scan", "--help"])
    assert "Usage" in result.output and "\x1b[" not in result.output
