"""End to end: real `git push` into a bare remote, through the hook `agentprint protect` adds.

The hook fires at git itself, so a push hidden from the agent hook (eval, a push built inside
`python -c`) is checked all the same. Everything lives in a temp folder with a throwaway home;
the hook runs this checkout's agentprint through the test's own Python.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

from agentprint import protect
from tests import fakes
from tests.conftest import set_home

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
    pytest.mark.posix_only,  # the hook runs through /bin/sh; Windows is covered in test_protect
]
ARGV = [sys.executable, "-c", "from agentprint.cli import app; app()"]


def git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True, text=True, timeout=120
    )


def commit(root, message):
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", message)


def remote_head(remote):
    done = git(remote, "rev-parse", "--verify", "-q", "refs/heads/main", check=False)
    return done.stdout.strip() or None


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("AGENTPRINT_STRICT", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "index.js").write_text("module.exports = 1;\n")
    commit(repo, "clean")
    git(repo, "remote", "add", "origin", str(remote))
    return repo, remote, home


def leak(repo):
    allow = [f"Bash(export ANTHROPIC_API_KEY={fakes.ANTHROPIC})"]
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": allow}})
    )
    commit(repo, "add agent settings")


def install(repo, home, argv=ARGV, strict=False):
    lines = []
    assert protect.protect(repo, home, lines.append, agentprint=argv, strict=strict) == 0, lines


def shell(repo, script, env=None):
    return subprocess.run(
        ["sh", "-c", script],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **(env or {})},
    )


def test_a_clean_push_goes_through(world):
    repo, remote, home = world
    install(repo, home)
    done = shell(repo, "git push -q origin HEAD:main")
    assert done.returncode == 0, done.stderr
    assert "agentprint: OK" in done.stderr
    assert remote_head(remote) == git(repo, "rev-parse", "HEAD").stdout.strip()


def test_a_leaky_push_is_blocked_with_the_key_masked(world):
    repo, remote, home = world
    leak(repo)
    install(repo, home)
    done = shell(repo, "git push origin HEAD:main")
    assert done.returncode != 0
    assert "agentprint: BLOCK" in done.stderr and "AP002" in done.stderr
    assert fakes.ANTHROPIC not in done.stderr + done.stdout
    assert fakes.ANTHROPIC[:4] + "…(len" in done.stderr
    assert remote_head(remote) is None


def test_an_eval_hidden_push_is_blocked(world):
    """The red-team case: text reading can't see through eval; git's own hook still fires."""
    repo, remote, home = world
    leak(repo)
    install(repo, home)
    done = shell(repo, "CMD='git push origin HEAD:main'; eval \"$CMD\"")
    assert done.returncode != 0
    assert "agentprint: BLOCK" in done.stderr
    assert remote_head(remote) is None


def test_a_push_built_inside_python_is_blocked(world):
    repo, remote, home = world
    leak(repo)
    install(repo, home)
    hidden = (
        "import subprocess, sys; "
        "sys.exit(subprocess.run(['git', 'pu' + 'sh', 'origin', 'HEAD:main']).returncode)"
    )
    done = subprocess.run(
        [sys.executable, "-c", hidden], cwd=repo, capture_output=True, text=True, timeout=120
    )
    assert done.returncode != 0
    assert "agentprint: BLOCK" in done.stderr
    assert remote_head(remote) is None


def test_deleting_a_remote_branch_is_never_blocked(world):
    repo, remote, home = world
    assert shell(repo, "git push -q origin HEAD:main").returncode == 0
    leak(repo)
    install(repo, home)
    done = shell(repo, "git push origin :main")
    assert done.returncode == 0, done.stderr
    assert remote_head(remote) is None


def test_an_existing_hook_runs_first_with_the_same_arguments_and_stdin(world, tmp_path):
    repo, remote, home = world
    log = tmp_path / "their-hook.log"
    theirs = repo / ".git" / "hooks" / "pre-push"
    theirs.write_text(f'#!/bin/sh\necho "args: $*" > "{log}"\ncat >> "{log}"\nexit 0\n')
    theirs.chmod(0o755)
    install(repo, home)
    done = shell(repo, "git push -q origin HEAD:main")
    assert done.returncode == 0, done.stderr
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert log.read_text() == f"args: origin {remote}\nHEAD {head} refs/heads/main {'0' * 40}\n"
    assert "agentprint: OK" in done.stderr  # ours ran too


def test_either_hook_failing_blocks_the_push(world):
    repo, remote, home = world
    theirs = repo / ".git" / "hooks" / "pre-push"
    theirs.write_text("#!/bin/sh\necho 'their hook says no' >&2\nexit 1\n")
    theirs.chmod(0o755)
    install(repo, home)
    done = shell(repo, "git push origin HEAD:main")  # clean repo: only their hook says no
    assert done.returncode != 0 and "their hook says no" in done.stderr
    theirs_now = repo / ".git" / "hooks" / protect.CHAINED
    theirs_now.write_text("#!/bin/sh\nexit 0\n")  # now theirs passes and ours blocks the leak
    leak(repo)
    done = shell(repo, "git push origin HEAD:main")
    assert done.returncode != 0 and "agentprint: BLOCK" in done.stderr
    assert remote_head(remote) is None


def test_a_missing_agentprint_lets_the_push_go_with_one_line(world, tmp_path):
    repo, remote, home = world
    leak(repo)
    install(repo, home, argv=[str(tmp_path / "no-such-agentprint")])
    done = shell(repo, "git push -q origin HEAD:main")
    assert done.returncode == 0, done.stderr
    assert (
        "agentprint: the push check could not run" in done.stderr and "push allowed" in done.stderr
    )
    assert remote_head(remote) is not None


@pytest.mark.parametrize("how", ["protect --strict", "AGENTPRINT_STRICT=1"])
def test_strict_blocks_when_agentprint_cannot_run(world, tmp_path, how):
    repo, remote, home = world
    install(
        repo, home, argv=[str(tmp_path / "no-such-agentprint")], strict=how.startswith("protect")
    )
    env = {"AGENTPRINT_STRICT": "1"} if how.startswith("AGENT") else {}
    done = shell(repo, "git push origin HEAD:main", env)
    assert done.returncode != 0
    assert "push blocked (strict mode)" in done.stderr
    assert remote_head(remote) is None


def test_after_undo_the_push_is_not_checked(world):
    repo, remote, home = world
    leak(repo)
    install(repo, home)
    assert protect.unprotect(repo, home, lambda line: None) == 0
    done = shell(repo, "git push -q origin HEAD:main")
    assert done.returncode == 0, done.stderr
    assert "agentprint" not in done.stderr
