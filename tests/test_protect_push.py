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
import time

import pytest

from agentprint import protect
from tests import fakes
from tests.conftest import set_home

# Git for Windows runs the hook with its own sh, so these run on Windows too; only the tests
# that need a POSIX shell of their own (eval, killing a hung check) are marked.
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
SH = shutil.which("sh") or shutil.which("bash")
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


def push(repo, *args, env=None):
    """A real `git push`, run directly (no shell in between)."""
    return subprocess.run(
        ["git", "push", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **(env or {})},
    )


def test_a_clean_push_goes_through(world):
    repo, remote, home = world
    install(repo, home)
    done = push(repo, "-q", "origin", "HEAD:main")
    assert done.returncode == 0, done.stderr
    assert "agentprint" not in done.stderr  # a clean push says nothing
    assert remote_head(remote) == git(repo, "rev-parse", "HEAD").stdout.strip()


def test_a_leaky_push_is_blocked_with_the_key_masked(world):
    repo, remote, home = world
    leak(repo)
    install(repo, home)
    done = push(repo, "origin", "HEAD:main")
    assert done.returncode != 0
    assert "agentprint: BLOCK" in done.stderr and "AP002" in done.stderr
    assert fakes.ANTHROPIC not in done.stderr + done.stdout
    assert fakes.ANTHROPIC[:4] + "…(len" in done.stderr
    assert remote_head(remote) is None


@pytest.mark.skipif(SH is None, reason="no sh")
def test_an_eval_hidden_push_is_blocked(world):
    """The red-team case: text reading can't see through eval; git's own hook still fires."""
    repo, remote, home = world
    leak(repo)
    install(repo, home)
    script = "CMD='git push origin HEAD:main'; eval \"$CMD\""
    done = subprocess.run(
        [SH or "sh", "-c", script], cwd=repo, capture_output=True, text=True, timeout=120
    )
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
    assert push(repo, "-q", "origin", "HEAD:main").returncode == 0
    leak(repo)
    install(repo, home)
    done = push(repo, "origin", ":main")
    assert done.returncode == 0, done.stderr
    assert remote_head(remote) is None


def test_an_existing_hook_runs_first_with_the_same_arguments_and_stdin(world, tmp_path):
    repo, remote, home = world
    log = tmp_path / "their-hook.log"
    theirs = repo / ".git" / "hooks" / "pre-push"
    at = log.as_posix()  # Git for Windows' sh reads C:/x, not C:\\x
    theirs.write_bytes(f'#!/bin/sh\necho "args: $*" > "{at}"\ncat >> "{at}"\nexit 0\n'.encode())
    theirs.chmod(0o755)
    install(repo, home)
    done = push(repo, "-q", "origin", "HEAD:main")
    assert done.returncode == 0, done.stderr
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert log.read_text() == f"args: origin {remote}\nHEAD {head} refs/heads/main {'0' * 40}\n"
    assert "agentprint" not in done.stderr  # ours ran too, and a clean push says nothing


def test_either_hook_failing_blocks_the_push(world):
    repo, remote, home = world
    theirs = repo / ".git" / "hooks" / "pre-push"
    theirs.write_bytes(b"#!/bin/sh\necho 'their hook says no' >&2\nexit 1\n")
    theirs.chmod(0o755)
    install(repo, home)
    done = push(repo, "origin", "HEAD:main")  # clean repo: only their hook says no
    assert done.returncode != 0 and "their hook says no" in done.stderr
    theirs_now = repo / ".git" / "hooks" / protect.CHAINED
    theirs_now.write_bytes(b"#!/bin/sh\nexit 0\n")  # now theirs passes and ours blocks the leak
    leak(repo)
    done = push(repo, "origin", "HEAD:main")
    assert done.returncode != 0 and "agentprint: BLOCK" in done.stderr
    assert remote_head(remote) is None


def test_a_missing_agentprint_lets_the_push_go_with_one_line(world, tmp_path):
    repo, remote, home = world
    leak(repo)
    install(repo, home, argv=[str(tmp_path / "no-such-agentprint")])
    done = push(repo, "-q", "origin", "HEAD:main")
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
    done = push(repo, "origin", "HEAD:main", env=env)
    assert done.returncode != 0
    assert "push blocked (strict mode)" in done.stderr
    assert remote_head(remote) is None


def test_after_undo_the_push_is_not_checked(world):
    repo, remote, home = world
    leak(repo)
    install(repo, home)
    assert protect.unprotect(repo, home, lambda line: None) == 0
    done = push(repo, "-q", "origin", "HEAD:main")
    assert done.returncode == 0, done.stderr
    assert "agentprint" not in done.stderr


# ---- what is checked is what git uploads, never the working tree ------------------------------


def remote_has(remote, ref):
    return git(remote, "rev-parse", "--verify", "-q", ref, check=False).stdout.strip() or None


def test_a_key_committed_then_fixed_only_on_disk_is_blocked(world):
    repo, remote, home = world
    server = {"command": "npx", "env": {"GITHUB_TOKEN": fakes.GITHUB}}
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": server}}))
    commit(repo, "shared mcp config")
    server["env"] = {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}  # fixed on disk, not committed
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": server}}))
    install(repo, home)
    done = push(repo, "origin", "HEAD:main")
    assert done.returncode != 0 and "agentprint: BLOCK" in done.stderr
    assert remote_head(remote) is None


def test_a_file_untracked_but_not_committed_is_blocked(world):
    repo, remote, home = world
    leak(repo)
    git(repo, "rm", "-q", "--cached", ".claude/settings.local.json")
    install(repo, home)
    done = push(repo, "origin", "HEAD:main")
    assert done.returncode != 0 and "agentprint: BLOCK" in done.stderr
    assert remote_head(remote) is None


def test_pushing_a_leaky_branch_from_a_clean_checkout_is_blocked(world):
    repo, remote, home = world
    git(repo, "checkout", "-q", "-b", "leaky")
    leak(repo)
    git(repo, "checkout", "-q", "-")
    assert not (repo / ".claude" / "settings.local.json").exists()
    install(repo, home)
    done = push(repo, "origin", "leaky")
    assert done.returncode != 0 and "agentprint: BLOCK" in done.stderr
    assert remote_has(remote, "refs/heads/leaky") is None


@pytest.mark.parametrize("annotated", [False, True])
def test_a_tag_on_a_leaky_commit_is_blocked(world, annotated):
    repo, remote, home = world
    git(repo, "checkout", "-q", "-b", "tmp")
    leak(repo)
    tag = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "tag"]
    git(repo, *tag, *(["-a", "-m", "v1"] if annotated else []), "v1")
    git(repo, "checkout", "-q", "-")
    install(repo, home)
    done = push(repo, "origin", "v1")
    assert done.returncode != 0 and "agentprint: BLOCK" in done.stderr
    assert remote_has(remote, "refs/tags/v1") is None


def test_an_uncommitted_key_that_is_not_pushed_does_not_block(world):
    repo, remote, home = world
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "npx"}}}))
    commit(repo, "shared mcp config")
    server = {"command": "npx", "env": {"GITHUB_TOKEN": fakes.GITHUB}}
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": server}}))  # not committed
    install(repo, home)
    done = push(repo, "origin", "HEAD:main")
    assert done.returncode == 0, done.stderr
    assert remote_head(remote) == git(repo, "rev-parse", "HEAD").stdout.strip()


def test_a_key_added_and_removed_on_the_way_is_blocked_until_the_commits_are_rewritten(world):
    repo, remote, home = world
    assert push(repo, "-q", "origin", "HEAD:main").returncode == 0
    pushed = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "branch", "-q", "-u", "origin/main")
    leak(repo)
    git(repo, "rm", "-q", "--cached", ".claude/settings.local.json")
    (repo / ".gitignore").write_text(".claude/settings.local.json\n")
    commit(repo, "untrack it")  # the newest tree is clean; the commit before holds the key
    install(repo, home)
    done = push(repo, "origin", "HEAD:main")
    assert done.returncode != 0 and "agentprint: BLOCK" in done.stderr
    assert "in pushed commit" in done.stderr and "git reset --soft" in done.stderr
    assert fakes.ANTHROPIC not in done.stderr
    assert remote_head(remote) == pushed
    git(repo, "reset", "-q", "--soft", pushed)  # what the brief says: rewrite, then push again
    commit(repo, "ignore local settings")
    done = push(repo, "origin", "HEAD:main")
    assert done.returncode == 0, done.stderr


@pytest.mark.posix_only  # stopping a hung native process from Git for Windows' sh is untested
@pytest.mark.parametrize("strict", [False, True])
def test_a_check_that_hangs_is_given_up_on(world, strict):
    repo, remote, home = world
    hang = [sys.executable, "-c", "import time; time.sleep(60)"]
    install(repo, home, argv=hang, strict=strict)
    started = time.monotonic()
    done = subprocess.run(
        ["git", "push", "origin", "HEAD:main"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "AGENTPRINT_HOOK_TIMEOUT": "2"},
    )
    assert time.monotonic() - started < 30
    assert "the push check took more than 2s" in done.stderr
    if strict:
        assert done.returncode != 0 and remote_head(remote) is None
    else:
        assert done.returncode == 0 and remote_head(remote) is not None


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
def test_a_clean_push_prints_nothing_under_bash_either(world):
    """bash (macOS's /bin/sh, Git for Windows' sh) reports a killed background job unless the
    hook waits for it: the time-limit watchdog must be reaped, or every push prints noise."""
    repo, remote, home = world
    install(repo, home)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    done = subprocess.run(
        [shutil.which("bash") or "bash", ".git/hooks/pre-push", "origin", str(remote)],
        cwd=repo,
        input=f"refs/heads/main {head} refs/heads/main {'0' * 40}\n",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0
    assert done.stderr == ""
