"""Windows-safe file writes and text I/O, tested on any OS (fake homes, fake errors only)."""

import io
import json
import os

import pytest

from agentprint import install, platform, safewrite
from agentprint.files import iter_lines

BOM = "﻿"


def _busy(code: int) -> PermissionError:
    error = PermissionError(13, "in use")
    error.winerror = code  # only set by the OS on Windows; the retry reads it by name
    return error


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    waited: list[float] = []
    monkeypatch.setattr(safewrite.time, "sleep", waited.append)
    return waited


def _fail_then_work(monkeypatch, times: int, code: int) -> list[int]:
    calls: list[int] = []
    real_replace = os.replace

    def fake(src, dst):
        calls.append(1)
        if len(calls) <= times:
            raise _busy(code)
        real_replace(src, dst)

    monkeypatch.setattr(safewrite.os, "replace", fake)
    return calls


@pytest.mark.parametrize("code", [5, 32])
def test_atomic_write_retries_a_file_in_use_and_then_succeeds(tmp_path, monkeypatch, sleeps, code):
    target = tmp_path / "settings.json"
    target.write_text("old")
    calls = _fail_then_work(monkeypatch, 2, code)
    safewrite.atomic_write(target, "new")
    assert target.read_text() == "new" and len(calls) == 3
    assert sleeps == [0.05, 0.1]
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]  # no temp file left


def test_atomic_write_gives_up_after_five_tries_and_leaves_the_file(tmp_path, monkeypatch, sleeps):
    target = tmp_path / "settings.json"
    target.write_text("old")
    calls = _fail_then_work(monkeypatch, 99, 32)
    with pytest.raises(safewrite.FileInUse, match="file in use, left as is"):
        safewrite.atomic_write(target, "new")
    assert len(calls) == 5 and sleeps == [0.05, 0.1, 0.2, 0.4]
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_a_plain_permission_error_on_posix_is_not_retried(tmp_path, monkeypatch, sleeps):
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    target = tmp_path / "f"
    target.write_text("old")

    def refuse(src, dst):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(safewrite.os, "replace", refuse)
    with pytest.raises(PermissionError) as info:
        safewrite.atomic_write(target, "new")
    assert not isinstance(info.value, safewrite.FileInUse) and sleeps == []


def test_any_permission_error_is_retried_on_windows(tmp_path, monkeypatch, sleeps):
    monkeypatch.setattr(platform, "is_windows", lambda: True)
    target = tmp_path / "f"
    target.write_text("old")
    calls = _fail_then_work(monkeypatch, 1, 0)  # winerror 0: still a PermissionError on Windows
    safewrite.atomic_write(target, "new")
    assert target.read_text() == "new" and len(calls) == 2


def test_install_reports_a_file_in_use_and_leaves_it_alone(tmp_path, monkeypatch, sleeps):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: ["/opt/agentprint"])
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    settings = home / ".gemini" / "settings.json"
    settings.write_text('{"theme": "dark"}\n')
    _fail_then_work(monkeypatch, 99, 32)
    out: list[str] = []
    install.install(home, {}, ["gemini"], False, out.append)
    assert "file in use, left as is" in "\n".join(out)
    assert settings.read_text() == '{"theme": "dark"}\n'


def test_a_utf8_bom_on_a_json_config_is_read_and_not_written_back(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: ["/opt/agentprint"])
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    settings = home / ".gemini" / "settings.json"
    original = (BOM + '{\n  "theme": "dark"\n}\n').encode()
    settings.write_bytes(original)
    out: list[str] = []
    install.install(home, {}, ["gemini"], False, out.append)
    after = settings.read_bytes()
    assert not after.startswith(BOM.encode())  # written without a BOM
    assert json.loads(after)["theme"] == "dark" and "mcpServers" in json.loads(after)
    assert "skipped" not in "\n".join(out)
    install.uninstall(home, {}, ["gemini"], False, out.append)
    assert settings.read_bytes() == original  # the backup puts the original bytes back


@pytest.mark.parametrize(
    ("name", "folder", "raw"),
    [
        ("settings.json", ".gemini", b'{\r\n  "theme": "dark"\r\n}\r\n'),
        ("config.toml", ".codex", b'model = "o3"\r\nsandbox = "read-only"\r\n'),
    ],
)
def test_install_edits_keep_a_files_crlf_line_ends(tmp_path, monkeypatch, name, folder, raw):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: ["/opt/agentprint"])
    home = tmp_path / "home"
    (home / folder).mkdir(parents=True)
    path = home / folder / name
    path.write_bytes(raw)
    names = ["gemini"] if folder == ".gemini" else ["codex"]
    install.install(home, {}, names, False, lambda line: None)
    after = path.read_bytes()
    assert after != raw and b"agentprint" in after
    assert after.count(b"\n") == after.count(b"\r\n")  # no bare LF crept in
    install.uninstall(home, {}, names, False, lambda line: None)
    assert path.read_bytes() == raw


def test_files_keep_their_line_ends_when_read_for_a_scan(tmp_path):
    path = tmp_path / "history.txt"
    path.write_bytes(b"one\r\ntwo\nthree\r\n")
    assert [text for _n, text in iter_lines(path)] == ["one\r\n", "two\n", "three\r\n"]


def test_a_cp1252_console_round_trips_non_ascii_json_after_utf8_streams(monkeypatch):
    payload = json.dumps({"command": "echo café ✓ 日本"}, ensure_ascii=False)
    raw_in, raw_out = io.BytesIO(payload.encode("utf-8")), io.BytesIO()
    stdin = io.TextIOWrapper(raw_in, encoding="cp1252")
    stdout = io.TextIOWrapper(raw_out, encoding="cp1252")
    monkeypatch.setattr(platform.sys, "stdin", stdin)
    monkeypatch.setattr(platform.sys, "stdout", stdout)
    monkeypatch.setattr(platform.sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    platform.use_utf8_streams()
    data = json.loads(platform.sys.stdin.read())
    print(json.dumps(data, ensure_ascii=False), file=platform.sys.stdout)
    platform.sys.stdout.flush()
    assert data["command"] == "echo café ✓ 日本"
    assert raw_out.getvalue().decode("utf-8").strip() == payload


def test_use_utf8_streams_leaves_streams_that_cannot_change_alone(monkeypatch):
    class Plain:
        pass

    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(platform.sys, name, Plain())
    platform.use_utf8_streams()  # no error


def test_guard_switches_to_utf8_before_reading(monkeypatch):
    from typer.testing import CliRunner

    from agentprint.cli import app

    seen: list[str] = []
    monkeypatch.setattr(platform, "use_utf8_streams", lambda: seen.append("utf8"))
    CliRunner().invoke(app, ["guard", "--agent", "claude-code"], input="{}")
    assert seen == ["utf8"]


def test_subprocess_kwargs_hide_the_console_window_only_on_windows(monkeypatch):
    monkeypatch.setattr(platform, "is_windows", lambda: True)
    assert platform.subprocess_kwargs() == {"creationflags": 0x08000000}
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    assert platform.subprocess_kwargs() == {}


def test_git_calls_read_utf8_and_do_not_quote_paths(tmp_path, monkeypatch):
    from agentprint import gitcheck
    from agentprint.checks import ap007_local_not_ignored as ap007

    seen: list[dict] = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def spy(cmd, **kwargs):
        seen.append({"cmd": cmd, **kwargs})
        return Done()

    monkeypatch.setattr(platform, "is_windows", lambda: True)
    monkeypatch.setattr(gitcheck.subprocess, "run", spy)
    monkeypatch.setattr(gitcheck.shutil, "which", lambda name: "git")
    gitcheck._git(tmp_path, "ls-files")
    monkeypatch.setattr(ap007.subprocess, "run", spy)
    ap007._git(tmp_path, "status")
    assert len(seen) == 2
    for call in seen:
        assert "core.quotepath=false" in call["cmd"]
        assert call["encoding"] == "utf-8" and call["errors"] == "replace"
        assert call["creationflags"] == 0x08000000
