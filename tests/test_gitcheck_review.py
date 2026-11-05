"""The push check never reports OK when it couldn't look (review item 10)."""

import shutil

import pytest

from agentprint import gitcheck
from agentprint.gitcheck import check_push
from agentprint.guard import evaluate
from agentprint.models import CannotCheck


def test_missing_folder_cannot_be_checked(tmp_path):
    with pytest.raises(CannotCheck):
        check_push(tmp_path / "typo")


def test_missing_git_cannot_be_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(gitcheck.shutil, "which", lambda name: None)
    with pytest.raises(CannotCheck):
        check_push(tmp_path)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_folder_outside_any_repo_is_simply_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert check_push(tmp_path) == []  # `git push` itself fails there; nothing to warn about


@pytest.mark.parametrize("command", ["cd typo && git push", 'cd "$REPO" && git push'])
def test_guard_says_could_not_check(tmp_path, command):
    verdict = evaluate(command, tmp_path)
    assert not verdict.block and "could not check this git push" in verdict.warning


def test_guard_without_git_says_could_not_check(tmp_path, monkeypatch):
    monkeypatch.setattr(gitcheck.shutil, "which", lambda name: None)
    verdict = evaluate("git push", tmp_path)
    assert not verdict.block and "could not check" in verdict.warning
