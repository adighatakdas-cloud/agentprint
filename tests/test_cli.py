import io
import json
import tarfile

from typer.testing import CliRunner

from agentprint import __version__
from agentprint.cli import app
from tests import fakes

runner = CliRunner()


def test_version_flag_prints_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_scan_json_end_to_end(fake_home, fake_project):
    result = runner.invoke(
        app, ["scan", str(fake_project), "--home", str(fake_home), "--format", "json"]
    )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["summary"] == {"critical": 3, "high": 5, "medium": 0, "low": 0}


def test_scan_never_prints_a_full_key(fake_home, fake_project):
    for fmt in ("table", "json"):
        result = runner.invoke(
            app, ["scan", str(fake_project), "--home", str(fake_home), "--format", fmt]
        )
        for key in fakes.ALL.values():
            assert key not in result.output


def test_scan_clean_project_without_home_exits_zero(tmp_path):
    result = runner.invoke(app, ["scan", str(tmp_path), "--no-home"])
    assert result.exit_code == 0
    assert "No findings" in result.output


def test_fail_on_none_always_exits_zero(fake_home, fake_project):
    args = ["scan", str(fake_project), "--home", str(fake_home), "--fail-on", "none"]
    assert runner.invoke(app, args).exit_code == 0


def test_pack_flags_leaky_tarball_exit_1(tmp_path):
    data = b'{"x": 1}'
    tgz = tmp_path / "p.tgz"
    with tarfile.open(tgz, "w:gz") as tf:
        info = tarfile.TarInfo("package/.claude/settings.local.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    result = runner.invoke(app, ["pack", str(tgz), "--format", "json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["findings"][0]["check_id"] == "AP006"


def test_pack_bad_target_exits_2(tmp_path):
    result = runner.invoke(app, ["pack", str(tmp_path / "missing")])
    assert result.exit_code == 2
    assert "does not exist" in result.output
