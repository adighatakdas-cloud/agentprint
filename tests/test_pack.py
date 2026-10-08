import io
import json
import shutil
import tarfile
import zipfile

import pytest

from agentprint.checks.ap006_package_leak import check_listing, is_agent_file
from agentprint.pack import PackError, collect
from tests import fakes

LEAKY = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}}).encode()


def _tgz(path, members: dict[str, bytes]):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def _findings(target):
    return [f for listing in collect(target, is_agent_file) for f in check_listing(listing)]


def test_tarball_with_claude_settings_is_flagged_with_secret(tmp_path):
    tgz = _tgz(
        tmp_path / "demo-1.0.0.tgz",
        {"package/index.js": b"module.exports = 1", "package/.claude/settings.local.json": LEAKY},
    )
    findings = _findings(tgz)
    assert [(f.check_id, f.severity.name) for f in findings] == [
        ("AP006", "HIGH"),
        ("AP002", "CRITICAL"),
    ]
    assert findings[0].location == "demo-1.0.0.tgz!package/.claude/settings.local.json"
    assert fakes.ANTHROPIC not in repr(findings)


def test_clean_tarball_has_no_findings(tmp_path):
    tgz = _tgz(
        tmp_path / "ok.tgz", {"package/index.js": b"1", "package/.claude/settings.json": b"{}"}
    )
    assert _findings(tgz) == []


def test_wheel_is_read_as_zip(tmp_path):
    whl = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("demo/__init__.py", "")
        zf.writestr("demo/.codex/config.toml", "model = 'x'")
    assert [f.check_id for f in _findings(whl)] == ["AP006"]


def test_archive_is_never_extracted_to_disk(tmp_path):
    tgz = _tgz(tmp_path / "evil.tgz", {"../../escape.txt": b"x"})
    _findings(tgz)
    assert not (tmp_path.parent / "escape.txt").exists()


def test_corrupt_archive_raises_pack_error(tmp_path):
    bad = tmp_path / "bad.tgz"
    bad.write_bytes(b"not a tarball")
    with pytest.raises(PackError):
        collect(bad, is_agent_file)


def test_directory_without_package_metadata_raises(tmp_path):
    with pytest.raises(PackError, match="package.json or pyproject.toml"):
        collect(tmp_path, is_agent_file)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_npm_project_dry_run(fake_project):
    findings = _findings(fake_project)
    assert tuple(f.check_id for f in findings) == ("AP006", "AP002")
    assert findings[0].location.endswith("!.claude/settings.local.json")


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_python_project_build(tmp_path):
    root = tmp_path / "pyproj"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    )
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_bytes(LEAKY)
    findings = _findings(root)
    assert any(f.check_id == "AP006" and ".tar.gz!" in f.location for f in findings)
