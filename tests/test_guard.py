import io
import json
import tarfile
import time
import zipfile

import pytest

from agentprint import guard
from agentprint.guard import evaluate
from tests import fakes

LEAKY = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}}).encode()


def _tgz(path, members):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def _wheel(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_ordinary_commands_are_not_our_business(tmp_path):
    assert evaluate("ls -la", tmp_path) is None
    assert evaluate("npm test", tmp_path) is None


def test_npm_publish_of_leaky_tarball_blocks_with_agent_reason(tmp_path):
    _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": LEAKY})
    verdict = evaluate("npm publish demo-1.0.0.tgz", tmp_path)
    assert verdict.block
    assert verdict.reason.startswith("agentprint: BLOCK — 2 problems to fix before publishing")
    assert "npm publish demo-1.0.0.tgz" in verdict.reason  # tells the agent what to re-run
    assert fakes.ANTHROPIC not in verdict.reason


def test_clean_publish_is_allowed(tmp_path):
    _tgz(tmp_path / "ok-1.0.0.tgz", {"package/index.js": b"1"})
    verdict = evaluate("npm publish ok-1.0.0.tgz", tmp_path)
    assert not verdict.block and verdict.warning is None


def test_uv_publish_checks_built_files_in_dist(tmp_path):
    _wheel(tmp_path / "dist" / "demo-1.0-py3-none-any.whl", {"demo/.codex/auth.json": "{}"})
    assert evaluate("uv publish", tmp_path).block
    assert evaluate("twine upload dist/*", tmp_path).block
    (tmp_path / "sub").mkdir()
    assert evaluate("cd .. && twine upload dist/*", tmp_path / "sub").block
    assert not evaluate("twine upload dist/*", tmp_path / "sub").block  # nothing built there


def test_python_publish_without_built_files_warns_but_allows(tmp_path):
    verdict = evaluate("poetry publish --build", tmp_path)
    assert not verdict.block
    assert "could not check" in verdict.warning


def test_git_push_runs_repo_check(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "check_push", lambda cwd: calls.append(cwd) or [])
    verdict = evaluate("cd sub && git push", tmp_path)
    assert calls == [tmp_path / "sub"] and not verdict.block


def test_internal_error_fails_open_with_visible_warning(tmp_path, monkeypatch):
    def boom(cwd):
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "check_push", boom)
    verdict = evaluate("git push", tmp_path)
    assert not verdict.block
    assert "internal error" in verdict.warning and "allowed" in verdict.warning


def test_unreadable_archive_warns_and_allows(tmp_path):
    (tmp_path / "broken.tgz").write_bytes(b"not a tarball")
    verdict = evaluate("npm publish broken.tgz", tmp_path)
    assert not verdict.block and "could not check" in verdict.warning


@pytest.mark.parametrize("command", ["ls", "npm publish ok-1.0.0.tgz"])
def test_guard_is_fast(tmp_path, command):
    _tgz(tmp_path / "ok-1.0.0.tgz", {"package/index.js": b"1"})
    start = time.perf_counter()
    evaluate(command, tmp_path)
    assert time.perf_counter() - start < 0.5
