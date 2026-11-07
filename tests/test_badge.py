"""--badge DIR on scan and pack: two small JSON files, and nothing else changes."""

import io
import json
import re
import tarfile

import pytest
from typer.testing import CliRunner

from agentprint import __version__, badge
from agentprint.cli import app
from agentprint.models import Finding, Severity
from agentprint.report import FailOn
from tests import fakes


def _finding(sev=Severity.HIGH):
    return Finding(
        "AP001", sev, "t", "claude", "/secret/path/file", 3, f"{fakes.ANTHROPIC[:4]}… (40)", "fix"
    )


def _same(a, b):
    """Output and exit code match; the table header's run time is the only thing that varies."""
    secs = re.compile(r"\d+\.\d\ds")
    return (secs.sub("Ns", a.output), a.exit_code) == (secs.sub("Ns", b.output), b.exit_code)


def _tarball(tmp_path):
    data = b'{"x": 1}'
    tgz = tmp_path / "p.tgz"
    with tarfile.open(tgz, "w:gz") as tf:
        info = tarfile.TarInfo("package/.claude/settings.local.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return tgz


def test_clear_badge_schema(tmp_path):
    b, v = badge.write(tmp_path, command="scan", fail_on=FailOn.high, findings=[])
    assert json.loads(b.read_text()) == {
        "schemaVersion": 1,
        "label": "agentprint",
        "message": "CLEAR",
        "color": "5CC8FF",
        "labelColor": "050B14",
    }
    assert json.loads(v.read_text()) == {
        "tool": "agentprint",
        "version": __version__,
        "command": "scan",
        "fail_on": "high",
        "verdict": "CLEAR",
        "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    }


def test_block_badge_on_findings_at_fail_on(tmp_path):
    b, v = badge.write(tmp_path, command="pack", fail_on=FailOn.high, findings=[_finding()])
    assert json.loads(b.read_text())["message"] == "BLOCK"
    assert json.loads(b.read_text())["color"] == "FF3B4E"
    data = json.loads(v.read_text())
    assert data["verdict"] == "BLOCK" and data["summary"]["high"] == 1
    # below the threshold stays CLEAR
    b2, _ = badge.write(
        tmp_path, command="scan", fail_on=FailOn.critical, findings=[_finding(Severity.HIGH)]
    )
    assert json.loads(b2.read_text())["message"] == "CLEAR"


def test_fail_on_none_is_clear(tmp_path):
    b, v = badge.write(
        tmp_path, command="scan", fail_on=FailOn.none, findings=[_finding(Severity.CRITICAL)]
    )
    assert json.loads(b.read_text())["message"] == "CLEAR"
    assert json.loads(v.read_text())["verdict"] == "CLEAR"


def test_verdict_has_no_paths_or_keys(fake_home, fake_project, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    r = CliRunner().invoke(
        app, ["scan", str(fake_project), "--home", str(fake_home), "--badge", str(out)]
    )
    assert r.exit_code == 1
    text = "".join(p.read_text() for p in out.iterdir())
    for secret in (fakes.ANTHROPIC, fakes.OPENAI, fakes.AWS):
        assert secret not in text and secret[:8] not in text
    assert str(tmp_path) not in text and "settings" not in text and ".claude" not in text
    assert json.loads((out / "badge.json").read_text())["message"] == "BLOCK"


def test_files_are_stable_json(tmp_path):
    b, v = badge.write(tmp_path, command="scan", fail_on=FailOn.high, findings=[_finding()])
    for path in (b, v):
        text = path.read_text()
        assert text.endswith("}\n") and not text.endswith("\n\n")
        data = json.loads(text)
        assert text == json.dumps(data, indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize("fmt", ["table", "agent", "json"])
def test_stdout_and_exit_code_unchanged_with_badge(fake_home, fake_project, tmp_path, fmt):
    out = tmp_path / "out"
    out.mkdir()
    scan = ["scan", str(fake_project), "--home", str(fake_home), "--format", fmt]
    plain = CliRunner().invoke(app, scan)
    with_badge = CliRunner().invoke(app, [*scan, "--badge", str(out)])
    assert _same(with_badge, plain)
    assert (out / "badge.json").is_file()
    tgz = _tarball(tmp_path)
    pack = ["pack", str(tgz), "--format", fmt]
    plain = CliRunner().invoke(app, pack)
    out2 = tmp_path / "out2"
    out2.mkdir()
    with_badge = CliRunner().invoke(app, [*pack, "--badge", str(out2)])
    assert _same(with_badge, plain)
    verdict = json.loads((out2 / "verdict.json").read_text())
    assert verdict["command"] == "pack"


@pytest.mark.parametrize("command", ["scan", "pack"])
def test_missing_dir_is_exit_2_with_message(fake_home, fake_project, tmp_path, command):
    missing = tmp_path / "nope"
    if command == "scan":
        args = ["scan", str(fake_project), "--home", str(fake_home), "--format", "agent"]
    else:
        args = ["pack", str(_tarball(tmp_path)), "--format", "agent"]
    r = CliRunner().invoke(app, [*args, "--badge", str(missing)])
    assert r.exit_code == 2
    assert r.output.startswith("agentprint: ")  # normal output printed first
    assert "error: " in r.output and "Traceback" not in r.output
    assert not missing.exists()
