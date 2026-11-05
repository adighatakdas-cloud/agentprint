import io
import json
import shutil
import tarfile
import warnings
import zipfile

import pytest

from agentprint.checks.ap006_package_leak import check_listing, is_agent_file
from agentprint.pack import PackError, collect, read_archive, read_archive_bytes
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


def test_truncated_archive_raises_pack_error(tmp_path):
    tgz = _tgz(tmp_path / "full.tgz", {"package/a.js": bytes(range(256)) * 400})
    cut = tmp_path / "cut.tgz"
    cut.write_bytes(tgz.read_bytes()[:200])
    with pytest.raises(PackError):
        collect(cut, is_agent_file)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_packaging_tool_timeout_raises_pack_error(fake_project, monkeypatch):
    monkeypatch.setattr("agentprint.pack.TIMEOUT_SECONDS", 0.001)
    with pytest.raises(PackError, match="timed out"):
        collect(fake_project, is_agent_file)


# --- Hardening for untrusted archives (downloaded packages) ---------------------------------


def _tar_with(path, entries):
    """entries: (TarInfo, data-or-None) pairs, written in order."""
    with tarfile.open(path, "w:gz") as tf:
        for info, data in entries:
            if data is None:
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return path


def _everything(name: str) -> bool:
    return True


def test_too_many_tar_members_raises_pack_error(tmp_path, monkeypatch):
    monkeypatch.setattr("agentprint.pack.MAX_MEMBERS", 3)
    tgz = _tgz(tmp_path / "many.tgz", {f"package/{i}.js": b"1" for i in range(5)})
    with pytest.raises(PackError, match="more than 3 entries"):
        read_archive(tgz, _everything)


def test_too_many_zip_members_raises_pack_error(tmp_path, monkeypatch):
    monkeypatch.setattr("agentprint.pack.MAX_MEMBERS", 3)
    whl = tmp_path / "many-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        for i in range(5):
            zf.writestr(f"demo/{i}.py", "")
    with pytest.raises(PackError, match="more than 3 entries"):
        read_archive(whl, _everything)


def test_tar_bomb_raises_pack_error(tmp_path, monkeypatch):
    # Listing a .tar.gz means unpacking every byte, so the unpacked total is capped too.
    monkeypatch.setattr("agentprint.pack.MAX_UNPACKED_BYTES", 1000)
    tgz = _tgz(tmp_path / "bomb.tgz", {"package/zeros.bin": bytes(5000)})
    with pytest.raises(PackError, match="unpacks to more than 1000 bytes"):
        read_archive(tgz, _everything)


def test_content_budget_stops_reading_but_keeps_listing(tmp_path, monkeypatch):
    monkeypatch.setattr("agentprint.pack.MAX_TOTAL_CONTENT_BYTES", 100)
    tgz = _tgz(tmp_path / "big.tgz", {f"package/{i}.txt": b"x" * 60 for i in range(3)})
    listing = read_archive(tgz, _everything)
    assert listing.files == ["package/0.txt", "package/1.txt", "package/2.txt"]
    assert list(listing.contents) == ["package/0.txt"]
    assert listing.skipped == ["package/1.txt", "package/2.txt"]


def test_oversized_member_is_listed_as_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr("agentprint.pack.MAX_CONTENT_BYTES", 10)
    whl = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("demo/.claude/settings.local.json", LEAKY)
    listing = read_archive(whl, is_agent_file)
    assert listing.contents == {}
    assert listing.skipped == ["demo/.claude/settings.local.json"]
    [finding] = check_listing(listing)
    assert finding.check_id == "AP006"
    assert "not scanned" in finding.detail


def test_tar_links_named_like_agent_files_are_reported(tmp_path):
    sym = tarfile.TarInfo("package/.claude/settings.local.json")
    sym.type, sym.linkname = tarfile.SYMTYPE, "/home/someone/.claude/settings.local.json"
    hard = tarfile.TarInfo("package/.codex/config.toml")
    hard.type, hard.linkname = tarfile.LNKTYPE, "package/index.js"
    tgz = _tar_with(
        tmp_path / "links.tgz",
        [(tarfile.TarInfo("package/index.js"), b"module.exports = 1"), (sym, None), (hard, None)],
    )
    listing = read_archive(tgz, is_agent_file)
    assert listing.files == [
        "package/index.js",
        "package/.claude/settings.local.json",
        "package/.codex/config.toml",
    ]
    assert listing.contents == {}  # links are never followed
    assert [f.check_id for f in check_listing(listing)] == ["AP006", "AP006"]


def test_zip_backslash_member_names_are_normalized(tmp_path):
    whl = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("demo\\.claude\\settings.local.json", LEAKY)
    listing = read_archive(whl, is_agent_file)
    assert listing.files == ["demo/.claude/settings.local.json"]
    assert [f.check_id for f in check_listing(listing)] == ["AP006", "AP002"]


def test_duplicate_tar_names_use_only_the_last_copy(tmp_path):
    name = "package/.claude/settings.local.json"
    tgz = _tar_with(
        tmp_path / "dup.tgz",
        [(tarfile.TarInfo(name), LEAKY), (tarfile.TarInfo(name), b"{}")],
    )
    listing = read_archive(tgz, is_agent_file)
    assert listing.files == [name]
    assert listing.contents == {name: b"{}"}  # what extraction leaves on disk; never mixed
    assert [f.check_id for f in check_listing(listing)] == ["AP006"]


def test_duplicate_zip_names_use_only_the_last_copy(tmp_path):
    whl = tmp_path / "demo-1.0-py3-none-any.whl"
    name = "demo/.claude/settings.local.json"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
        with zipfile.ZipFile(whl, "w") as zf:
            zf.writestr(name, b"{}")
            zf.writestr(name, LEAKY)
    listing = read_archive(whl, is_agent_file)
    assert listing.files == [name]
    assert listing.contents == {name: LEAKY}


def test_archive_bytes_are_read_in_memory():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("demo/.claude/settings.local.json", LEAKY)
    listing = read_archive_bytes(buf.getvalue(), "demo-1.0-py3-none-any.whl", is_agent_file)
    assert listing.label == "demo-1.0-py3-none-any.whl"
    assert listing.contents == {"demo/.claude/settings.local.json": LEAKY}
