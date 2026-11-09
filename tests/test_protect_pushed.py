"""What `gitcheck.check_pushed` reads for a push: real git objects in a temp repository.

Each case is a hole a reviewer found: a lockfile history that used up the read budget and threw
away a key found at the tip, a tag on a single file that dropped the other refs, a key brought
in by a merge, and a commit that only another remote has. Histories are built with
`git fast-import`, so a few hundred commits take well under a second.
"""

import json
import shutil
import subprocess

import pytest

from agentprint import gitcheck
from agentprint.gitcheck import check_pushed, parse_updates
from tests import fakes
from tests.conftest import set_home

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
ZERO = "0" * 40
SETTINGS = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}})
MCP = json.dumps({"mcpServers": {"x": {"command": "npx", "env": {"T": fakes.GITHUB}}}})


def git(root, *args, stdin=None):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        input=stdin,
        timeout=120,
    ).stdout.strip()


def sha_of(repo, rev):
    done = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return done.stdout.strip() or None


def fast_import(repo, commits, branch="main"):
    """Commits on refs/heads/<branch> (from that branch, else from HEAD), each a
    (message, {path: text, or None to delete}). Returns the new tip."""
    parent = sha_of(repo, f"refs/heads/{branch}") or sha_of(repo, "HEAD")
    out = []
    for i, (message, files) in enumerate(commits):
        out.append(f"commit refs/heads/{branch}\n")
        out.append("committer t <t@example.invalid> 1700000000 +0000\n")
        out.append(f"data {len(message.encode())}\n{message}\n")
        if i == 0 and parent:
            out.append(f"from {parent}\n")
        for path, text in files.items():
            if text is None:
                out.append(f"D {path}\n")
            else:
                data = text.encode()
                out.append(f"M 100644 inline {path}\ndata {len(data)}\n{text}\n")
    # Bytes, not text: on Windows a text-mode pipe turns \n into \r\n and breaks `data <len>`.
    subprocess.run(
        ["git", "-C", str(repo), "fast-import", "--quiet", "--force"],
        check=True,
        capture_output=True,
        input="".join(out).encode(),
        timeout=120,
    )
    if branch == "main":
        git(repo, "reset", "-q", "--hard", branch)
    return sha_of(repo, f"refs/heads/{branch}")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    set_home(monkeypatch, tmp_path / "home")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    fast_import(root, [("start", {"index.js": "module.exports = 1;"})])
    return root


def update(sha, ref="refs/heads/main", old=ZERO):
    return parse_updates(f"{ref} {sha} {ref} {old}\n")


def keys(findings):
    return [f for f in findings if f.check_id in ("AP002", "AP005")]


def test_a_lockfile_history_is_never_read_so_a_key_at_the_tip_is_found(repo, monkeypatch):
    """C1: 200 lockfile bumps used to fill the read budget and throw away the tip's key."""
    monkeypatch.setattr(gitcheck, "MAX_READ_BYTES", 256 * 1024)  # far below 200 lockfiles
    lock = "x" * 20_000
    history = [(f"bump {i}", {"package-lock.json": f"{i}{lock}"}) for i in range(200)]
    fast_import(repo, history)
    head = fast_import(repo, [("leak", {".claude/settings.local.json": SETTINGS})])
    findings, notes = check_pushed(repo, update(head))
    assert notes == []
    assert [f.check_id for f in keys(findings)] == ["AP002"]


def test_a_tag_on_a_single_file_keeps_the_other_refs_findings(repo):
    """X19: one ref that can't be read is a note; the leaky branch beside it still counts."""
    head = fast_import(repo, [("leak", {".mcp.json": MCP})], branch="leaky")
    blob = git(repo, "hash-object", "-w", "--stdin", stdin="hello\n")
    updates = parse_updates(
        f"refs/heads/leaky {head} refs/heads/leaky {ZERO}\n"
        f"refs/tags/zz-blob {blob} refs/tags/zz-blob {ZERO}\n"
    )
    findings, notes = check_pushed(repo, updates)
    assert [f.check_id for f in keys(findings)] == ["AP005"]
    assert notes == [
        "refs/tags/zz-blob points at a single file, not a commit, so agentprint can't tell "
        "which file it is"
    ]


def test_running_out_of_budget_keeps_what_was_found_and_says_so(repo, monkeypatch):
    head = fast_import(
        repo,
        [
            ("old key", {".mcp.json": MCP}),
            ("drop it", {".mcp.json": None}),
            ("tip key", {".claude/settings.local.json": SETTINGS}),
        ],
    )
    monkeypatch.setattr(gitcheck, "MAX_READ_BYTES", len(SETTINGS) + 10)  # the tip fits, no more
    findings, notes = check_pushed(repo, update(head))
    assert [f.check_id for f in keys(findings)] == ["AP002"]
    assert len(notes) == 1 and "some were not read" in notes[0]


def test_a_key_a_merge_brings_in_is_read(repo):
    """diff-tree shows a merge's own changes only with -m: a key added in the merge itself,
    then deleted, used to be missed."""
    base = git(repo, "rev-parse", "HEAD")
    side = fast_import(repo, [("side", {"side.txt": "s"})], branch="side")
    git(repo, "checkout", "-q", "main")
    git(repo, "reset", "-q", "--hard", base)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "merge", "-q",
        "--no-ff", "--no-commit", side)  # fmt: skip
    (repo / ".mcp.json").write_text(MCP)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "merge")
    merge = git(repo, "rev-parse", "HEAD")
    head = fast_import(repo, [("drop it", {".mcp.json": None})])
    findings, _ = check_pushed(repo, update(head))
    assert [f.check_id for f in keys(findings)] == ["AP005"]
    assert f"in pushed commit {merge[:12]}" in keys(findings)[0].detail


def test_commits_another_remote_has_are_still_read(repo, tmp_path):
    """I1: a private mirror having the commits doesn't make them public: only what the remote
    being pushed to has is left out."""
    head = fast_import(
        repo,
        [
            ("leak", {".claude/settings.local.json": SETTINGS}),
            ("untrack", {".claude/settings.local.json": None, ".gitignore": ".claude/"}),
        ],
    )
    for name in ("private", "origin"):
        git(tmp_path, "init", "-q", "--bare", str(tmp_path / f"{name}.git"))
        git(repo, "remote", "add", name, str(tmp_path / f"{name}.git"))
    git(repo, "push", "-q", "--no-verify", "private", "main")
    findings, _ = check_pushed(repo, update(head), remote="origin")
    assert [f.check_id for f in keys(findings)] == ["AP002"]
    assert "in pushed commit" in keys(findings)[0].detail
    findings, _ = check_pushed(repo, update(head), remote="private")  # it has them all
    assert keys(findings) == []


def test_a_push_to_a_url_leaves_out_only_the_remote_s_old_commit(repo, tmp_path):
    head = fast_import(
        repo,
        [
            ("leak", {".claude/settings.local.json": SETTINGS}),
            ("untrack", {".claude/settings.local.json": None, ".gitignore": ".claude/"}),
        ],
    )
    git(repo, "update-ref", "refs/remotes/origin/main", head)  # a tracking ref of some remote
    url = str(tmp_path / "somewhere.git")
    findings, _ = check_pushed(repo, update(head), remote=url)
    assert [f.check_id for f in keys(findings)] == ["AP002"]


def test_more_commits_than_it_reads_is_a_note(repo, monkeypatch):
    monkeypatch.setattr(gitcheck, "MAX_COMMITS", 5)
    head = fast_import(repo, [(f"c{i}", {"src/n.txt": str(i)}) for i in range(10)])
    _, notes = check_pushed(repo, update(head))
    assert len(notes) == 1 and "more than 5 commits" in notes[0]
