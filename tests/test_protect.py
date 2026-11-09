"""`agentprint protect`: the git pre-push hook and npm's prepublishOnly, added and taken out.

Every repository, home and package.json here is built in a temp folder; nothing touches the
real home or this repository's own hooks. The end-to-end pushes are in test_protect_push.py.
"""

import json
import os
import shutil
import stat
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from agentprint import __version__, protect
from agentprint.cli import app
from agentprint.gitcheck import RefUpdate, parse_updates
from agentprint.models import CannotCheck
from agentprint.protect import (
    CHAINED,
    MARK,
    guarded_snippet,
    hook_script,
    npm_command,
    push_check,
)
from tests import fakes
from tests.conftest import set_home

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
ARGV = [sys.executable, "-c", "from agentprint.cli import app; app()"]
ZERO = "0" * 40
SHA = "a" * 40


class Out(list):
    def __call__(self, line):
        self.append(line)

    @property
    def text(self):
        return "\n".join(self)


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def home(tmp_path, monkeypatch):
    # Keep the machine's own git config out, and make a throwaway home the user's home.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def repo(tmp_path, home):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root


def listing(folder):
    """Every file under `folder` with its bytes and mode, to compare before and after."""
    return {
        str(p.relative_to(folder)): (p.read_bytes(), stat.S_IMODE(p.lstat().st_mode))
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def run(path, home, **kw):
    out = Out()
    code = protect.protect(path, home, out, agentprint=ARGV, **kw)
    return code, out.text


def undo(path, home):
    out = Out()
    return protect.unprotect(path, home, out), out.text


# ---- the hook script --------------------------------------------------------------------------


def test_hook_script_is_lf_only_posix_sh_on_every_os(monkeypatch):
    """Git for Windows runs hooks with its bundled sh: LF line ends, forward-slash paths."""
    text = hook_script(["/opt/bin/agentprint"], strict=False)
    assert text.startswith("#!/bin/sh\n") and "\r" not in text and MARK in text
    assert '| /opt/bin/agentprint push-check > "$tmp" &\n' in text
    monkeypatch.setattr(protect.platform, "is_windows", lambda: True)
    windows = hook_script([r"C:\Users\me\.local\bin\agentprint.exe"], strict=True)
    assert '| C:/Users/me/.local/bin/agentprint.exe push-check --strict > "$tmp" &\n' in windows
    assert "\\" not in windows.replace("\\n", "")
    assert "strict=1" in windows


@pytest.mark.posix_only
@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
def test_hook_script_is_valid_sh():
    for strict in (False, True):
        done = subprocess.run(["sh", "-n"], input=hook_script(ARGV, strict), text=True)
        assert done.returncode == 0


# ---- git: add, chain, undo --------------------------------------------------------------------


@needs_git
def test_protect_adds_a_hook_and_undo_takes_it_out_exactly(repo, home):
    hooks = repo / ".git" / "hooks"
    before = listing(hooks)
    code, text = run(repo, home)
    assert code == 0, text
    hook = hooks / "pre-push"
    assert MARK in hook.read_text() and "push-check" in hook.read_text()
    if os.name != "nt":
        assert os.access(hook, os.X_OK)
    assert "added a pre-push hook" in text and "agentprint protect --undo" in text
    assert (home / ".agentprint" / "protect.json").is_file()
    code, text = undo(repo, home)
    assert code == 0, text
    assert listing(hooks) == before
    assert not (home / ".agentprint").exists()  # protect created it; nothing else is in it


@needs_git
def test_a_missing_hooks_folder_is_made_and_removed_again(repo, home):
    shutil.rmtree(repo / ".git" / "hooks")
    assert run(repo, home)[0] == 0
    assert (repo / ".git" / "hooks" / "pre-push").is_file()
    assert undo(repo, home)[0] == 0
    assert not (repo / ".git" / "hooks").exists()


@needs_git
def test_an_existing_hook_is_kept_byte_for_byte_and_restored(repo, home):
    hooks = repo / ".git" / "hooks"
    theirs = hooks / "pre-push"
    original = b"#!/bin/sh\r\n# mine\r\necho 'their hook' >&2\r\nexit 0"  # CRLF, no final newline
    theirs.write_bytes(original)
    theirs.chmod(0o750)
    before = listing(hooks)
    code, text = run(repo, home)
    assert code == 0, text
    assert (hooks / CHAINED).read_bytes() == original
    assert listing(hooks)[CHAINED] == before["pre-push"]  # same bytes, same mode
    assert MARK in theirs.read_text()
    assert "kept as" in text and CHAINED in text
    assert undo(repo, home)[0] == 0
    assert listing(hooks) == before


@needs_git
def test_a_failed_write_puts_your_own_hook_back(repo, home, monkeypatch):
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-push").write_bytes(b"#!/bin/sh\nexit 0\n")
    before = listing(hooks)

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(protect, "atomic_write", full_disk)
    code, text = run(repo, home)
    assert code == 1 and "git: stopped" in text
    assert listing(hooks) == before
    assert not (home / ".agentprint" / "protect.json").exists()


@needs_git
def test_protect_twice_changes_nothing_and_strict_rewrites_in_place(repo, home):
    hooks = repo / ".git" / "hooks"
    before = listing(hooks)
    run(repo, home)
    first = (hooks / "pre-push").read_bytes()
    code, text = run(repo, home)
    assert code == 0 and "already protected" in text
    assert (hooks / "pre-push").read_bytes() == first
    code, text = run(repo, home, strict=True)
    assert code == 0 and "updated" in text and "strict" in text
    assert b"strict=1" in (hooks / "pre-push").read_bytes()
    assert undo(repo, home)[0] == 0
    assert listing(hooks) == before


@needs_git
def test_undo_leaves_a_hook_changed_since_and_says_what_to_remove(repo, home):
    hook = repo / ".git" / "hooks" / "pre-push"
    run(repo, home)
    edited = hook.read_bytes() + b"# my own line\n"
    hook.write_bytes(edited)
    code, text = undo(repo, home)
    assert code == 1
    assert hook.read_bytes() == edited
    assert "changed since" in text and "by hand" in text


@needs_git
def test_undo_leaves_both_files_when_the_chained_hook_changed_since(repo, home):
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    run(repo, home)
    ours = (hooks / "pre-push").read_bytes()
    (hooks / CHAINED).write_text("#!/bin/sh\nexit 3\n")
    code, text = undo(repo, home)
    assert code == 1 and "by hand" in text
    assert (hooks / "pre-push").read_bytes() == ours
    assert (hooks / CHAINED).read_text() == "#!/bin/sh\nexit 3\n"


@needs_git
def test_a_leftover_chained_file_stops_protect(repo, home):
    hooks = repo / ".git" / "hooks"
    (hooks / CHAINED).write_text("old\n")
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    code, text = run(repo, home)
    assert code == 1 and "already exists" in text
    assert (hooks / "pre-push").read_text() == "#!/bin/sh\nexit 0\n"


@needs_git
def test_undo_without_a_record_says_what_to_remove(repo, home):
    run(repo, home)
    (home / ".agentprint" / "protect.json").unlink()
    code, text = undo(repo, home)
    assert code == 1 and "no record" in text and "delete" in text
    assert MARK in (repo / ".git" / "hooks" / "pre-push").read_text()


@needs_git
def test_a_worktree_puts_the_hook_where_git_reads_it(repo, home, tmp_path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "a")
    tree = tmp_path / "tree"
    git(repo, "worktree", "add", "-q", str(tree))
    assert run(tree, home)[0] == 0
    assert MARK in (repo / ".git" / "hooks" / "pre-push").read_text()
    assert undo(tree, home)[0] == 0
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


DISPATCH = '#!/bin/sh\nhook=$(basename "$0")\n[ "$hook" = pre-push ] || exit 1\nexit 0\n'


@needs_git
@pytest.mark.parametrize(
    "content, manager",
    [
        (DISPATCH, "own file name"),
        ('#!/bin/sh\n. "${0%/*}/../../.husky/_/husky.sh"\n', "husky"),
        ("#!/bin/sh\n# overcommit hook\nexec ruby run-hook\n", "overcommit"),
        ('#!/bin/sh\nexec lefthook run "pre-push" "$@"\n', "lefthook"),
        ("#!/usr/bin/env bash\n# File generated by pre-commit\nexec python -mx\n", "pre-commit"),
    ],
)
def test_a_hook_that_would_break_when_renamed_is_left_alone(repo, home, content, manager):
    """Renamed to pre-push.agentprint-chained, a hook that reads its own name ($0) stops working
    (this one would fail every push), and a manager's hook is the manager's to change."""
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-push").write_text(content)
    before = listing(hooks)
    code, text = run(repo, home)
    assert code == 1
    assert listing(hooks) == before
    assert manager in text and "left it as is" in text
    if manager == "pre-commit":
        assert "stages: [pre-push]" in text and "pre-commit install --hook-type pre-push" in text
        assert f'additional_dependencies: ["agentprint=={__version__}"]' in text
    else:
        assert f"out=$(uvx agentprint=={__version__} push-check) || s=$?" in text
    if manager == "lefthook":
        assert "use_stdin: true" in text
    assert not (home / ".agentprint" / "protect.json").exists()


@needs_git
def test_push_check_reads_the_push_from_the_pre_commit_framework(repo, monkeypatch):
    leak = {"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}}
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(json.dumps(leak))
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "rm", "-q", "--cached", ".claude/settings.local.json")  # clean index
    env = {"PRE_COMMIT_TO_REF": head, "PRE_COMMIT_FROM_REF": ZERO}
    assert push_check(repo, "", env)[0] == 1  # read from the commit, not the clean index
    assert push_check(repo, "", {})[0] == 0


# ---- core.hooksPath ---------------------------------------------------------------------------


@needs_git
def test_husky_hooks_path_inside_the_repo_prints_the_line_instead(repo, home):
    (repo / ".husky" / "_").mkdir(parents=True)
    (repo / ".husky" / "pre-push").write_text("npm test\n")
    git(repo, "config", "core.hooksPath", ".husky/_")
    before = listing(repo)
    code, text = run(repo, home)
    assert code == 1
    assert listing(repo) == before  # nothing written anywhere in the repository
    assert "core.hooksPath" in text and ".husky/pre-push" in text.replace(os.sep, "/")
    assert f"    s=0; out=$(uvx agentprint=={__version__} push-check) || s=$?" in text
    assert "committed" in text and "push goes on with one line" in text
    assert not (home / ".agentprint" / "protect.json").exists()


@needs_git
def test_lefthook_gets_its_yaml(repo, home):
    (repo / "lefthook.yml").write_text("pre-commit:\n  jobs: []\n")
    (repo / "githooks").mkdir()
    git(repo, "config", "core.hooksPath", "githooks")
    code, text = run(repo, home, strict=True)
    assert code == 1
    assert "lefthook.yml" in text and "lefthook install" in text
    assert "          run: |\n" in text and "use_stdin: true" in text
    assert f"            s=0; out=$(uvx agentprint=={__version__} push-check --strict)" in text
    assert "push blocked" in text
    assert not (repo / "githooks" / "pre-push").exists()


@needs_git
def test_a_tracked_hooks_folder_gets_the_line(repo, home):
    (repo / "scripts" / "hooks").mkdir(parents=True)
    git(repo, "config", "core.hooksPath", "scripts/hooks")
    code, text = run(repo, home)
    assert code == 1 and "a folder inside the repository" in text
    assert f"uvx agentprint=={__version__} push-check" in text
    assert list((repo / "scripts" / "hooks").iterdir()) == []


@needs_git
@pytest.mark.posix_only
def test_a_hooks_folder_outside_the_repo_that_you_own_is_written(repo, home, tmp_path):
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    git(repo, "config", "core.hooksPath", str(shared))
    code, text = run(repo, home)  # other repositories may use it: only with --global
    assert code == 1 and "agentprint protect --global" in text
    assert list(shared.iterdir()) == []
    code, text = run(repo, home, shared=True)
    assert code == 0, text
    assert MARK in (shared / "pre-push").read_text()
    assert "every repository that uses that folder" in text
    assert undo(repo, home)[0] == 0
    assert list(shared.iterdir()) == []


@needs_git
def test_a_hooks_folder_outside_the_repo_you_do_not_own_gets_the_line(
    repo, home, tmp_path, monkeypatch
):
    shared = tmp_path / "theirs"
    shared.mkdir()
    git(repo, "config", "core.hooksPath", str(shared))
    monkeypatch.setattr(protect, "_owned_dir", lambda folder: False)
    code, text = run(repo, home)
    assert code == 1 and "a folder you don't own" in text
    assert list(shared.iterdir()) == []


# ---- npm: prepublishOnly ----------------------------------------------------------------------

BASE = {"name": "demo", "version": "1.0.0", "files": ["index.js"], "license": "MIT"}
VARIANTS = {
    "2-space": (json.dumps(BASE, indent=2) + "\n", "  "),
    "4-space": (json.dumps(BASE, indent=4) + "\n", "    "),
    "tab": (json.dumps(BASE, indent="\t") + "\n", "\t"),
    "crlf": ((json.dumps(BASE, indent=2) + "\n").replace("\n", "\r\n"), "  "),
    "scripts": (json.dumps({**BASE, "scripts": {"test": "node --test"}}, indent=2) + "\n", "  "),
    "no-final-newline": (json.dumps(BASE, indent=2), "  "),
}


@pytest.fixture
def package(tmp_path, home):
    folder = tmp_path / "pkg"
    folder.mkdir()
    return folder


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_prepublish_only_added_keeping_indent_order_and_line_ends(package, home, variant):
    original, indent = VARIANTS[variant]
    path = package / "package.json"
    path.write_bytes(original.encode())
    code, text = run(package, home)
    assert code == 0, text
    raw = path.read_bytes().decode()
    data = json.loads(raw)
    assert data["scripts"]["prepublishOnly"] == npm_command()
    assert list(data)[: len(BASE)] == list(BASE)  # key order kept; scripts last if new
    assert raw.splitlines()[1].startswith(indent) and not raw.splitlines()[1].startswith(
        indent + indent[0]
    )
    if variant == "crlf":
        assert raw.count("\r\n") == raw.count("\n")
    else:
        assert "\r" not in raw
    if variant == "scripts":
        assert list(data["scripts"]) == ["test", "prepublishOnly"]
    assert "prepublishOnly" in text and npm_command() in text
    assert undo(package, home)[0] == 0
    assert path.read_bytes() == original.encode()


def test_undo_restores_package_json_byte_for_byte(package, home):
    original = '{"name":"demo",\n  "version": "1.0.0", "keywords": ["a", "b"]}'
    path = package / "package.json"
    path.write_text(original, newline="")
    assert run(package, home)[0] == 0
    assert path.read_bytes() != original.encode()
    code, text = undo(package, home)
    assert code == 0 and "back exactly" in text
    assert path.read_bytes() == original.encode()


def test_an_existing_prepublish_only_is_kept_and_runs_first(package, home):
    path = package / "package.json"
    original = json.dumps({"name": "x", "scripts": {"prepublishOnly": "npm test"}}, indent=2)
    path.write_text(original + "\n")
    assert run(package, home)[0] == 0
    script = json.loads(path.read_text())["scripts"]["prepublishOnly"]
    assert script == f"npm test && {npm_command()}"
    code, text = run(package, home)
    assert code == 0 and "already protected" in text
    assert json.loads(path.read_text())["scripts"]["prepublishOnly"] == script
    assert undo(package, home)[0] == 0
    assert path.read_text() == original + "\n"


def test_protect_again_refreshes_an_older_pin_and_undo_still_restores(package, home, monkeypatch):
    path = package / "package.json"
    original = json.dumps({"name": "x", "scripts": {"prepublishOnly": "npm test"}}, indent=2)
    path.write_text(original + "\n")
    monkeypatch.setattr(protect, "__version__", "0.0.9")
    run(package, home)
    assert "agentprint-cli@0.0.9 pack" in path.read_text()
    monkeypatch.undo()
    code, text = run(package, home)
    script = json.loads(path.read_text())["scripts"]["prepublishOnly"]
    assert script == f"npm test && {npm_command()}"
    assert code == 0 and "0.0.9" in text  # the diff shows the old pin going
    assert undo(package, home)[0] == 0
    assert path.read_text() == original + "\n"


def test_undo_leaves_a_package_json_changed_since(package, home):
    path = package / "package.json"
    path.write_text(json.dumps({"name": "x"}, indent=2) + "\n")
    run(package, home)
    data = json.loads(path.read_text())
    data["version"] = "2.0.0"
    path.write_text(json.dumps(data, indent=2) + "\n")
    edited = path.read_bytes()
    code, text = undo(package, home)
    assert code == 1 and "changed since" in text and "prepublishOnly" in text
    assert path.read_bytes() == edited
    assert not (home / ".agentprint" / "protect.json").exists()  # no copy of it kept


def test_a_package_json_it_cannot_edit_is_left_with_the_snippet(package, home):
    path = package / "package.json"
    path.write_text('{\n  // a comment\n  "name": "x"\n}\n')
    code, text = run(package, home)
    assert code == 1 and "left as is" in text and npm_command() in text
    assert path.read_text() == '{\n  // a comment\n  "name": "x"\n}\n'


def test_a_folder_with_nothing_to_protect(tmp_path, home):
    code, text = run(tmp_path, home)
    assert code == 2 and "nothing to protect" in text


def test_yarn_berry_and_python_get_a_note(package, home):
    (package / "package.json").write_text(json.dumps({"packageManager": "yarn@4.5.0"}) + "\n")
    (package / "pyproject.toml").write_text("[project]\nname='x'\n")
    code, text = run(package, home)
    assert code == 0
    assert "Yarn 2+" in text and "does not run prepublishOnly" in text
    assert "PyPI" in text and "no publish hook" in text


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_npm_reads_the_edited_package_json(package, home, monkeypatch):
    (package / "package.json").write_bytes(b'{\r\n\t"name": "x",\r\n\t"version": "1.0.0"\r\n}')
    run(package, home)
    monkeypatch.setenv("NPM_CONFIG_UPDATE_NOTIFIER", "false")
    done = subprocess.run(
        [shutil.which("npm") or "npm", "pkg", "get", "scripts.prepublishOnly"],
        cwd=package,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == npm_command()


def test_the_shared_npm_script_lets_the_publish_go_where_agentprint_cannot_run(package, home):
    path = package / "package.json"
    path.write_text('{"name": "x"}\n')
    code, text = run(package, home)
    script = json.loads(path.read_text())["scripts"]["prepublishOnly"]
    assert script.endswith("pack --fail-on high --if-unavailable allow")
    assert "teammates" in text and "goes on with one line" in text
    undo(package, home)
    code, text = run(package, home, strict=True)
    script = json.loads(path.read_text())["scripts"]["prepublishOnly"]
    assert script == f"npx --yes agentprint-cli@{__version__} pack --fail-on high"
    assert "publish stops (strict)" in text


def test_pack_if_unavailable_allow_exits_0_with_one_line(tmp_path):
    missing = str(tmp_path / "no-such-package")
    blocked = CliRunner().invoke(app, ["pack", missing, "--format", "agent"])
    assert blocked.exit_code == 2
    allowed = CliRunner().invoke(
        app, ["pack", missing, "--format", "agent", "--if-unavailable", "allow"]
    )
    assert allowed.exit_code == 0
    assert "could not check this package" in allowed.output and "publish goes on" in allowed.output


FAKE_UVX = """#!/bin/sh
printf '%s\\n' "$FAKE_OUT"
exit "${FAKE_EXIT:-0}"
"""


@pytest.mark.posix_only
@pytest.mark.parametrize(
    "uvx, out, code, strict, expected",
    [
        (False, "", 0, False, 0),  # no uv on this machine: one line, the push goes on
        (False, "", 0, True, 1),  # strict: blocked
        (True, "agentprint: BLOCK — 1 problem to fix before pushing", 1, False, 1),
        (True, "Traceback (most recent call last)", 1, False, 0),  # a crash is not a BLOCK
        (True, "agentprint: OK", 0, False, 0),
    ],
)
def test_the_hook_manager_snippet_fails_open_under_sh_e(tmp_path, uvx, out, code, strict, expected):
    """husky runs .husky/pre-push with `sh -e`: the snippet must survive a failing agentprint."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if uvx:
        (bin_dir / "uvx").write_text(FAKE_UVX)
        (bin_dir / "uvx").chmod(0o755)
    script = tmp_path / "pre-push"
    script.write_text("\n".join(["#!/bin/sh", *guarded_snippet(strict), "echo after >&2", ""]))
    env = {"PATH": str(bin_dir), "FAKE_OUT": out, "FAKE_EXIT": str(code)}
    sh = shutil.which("sh") or "/bin/sh"
    done = subprocess.run([sh, "-e", str(script)], env=env, capture_output=True, text=True)
    assert done.returncode == expected, done.stderr
    if not uvx:
        assert f"push {'blocked' if strict else 'allowed'}" in done.stderr
    if out == "agentprint: OK":
        assert done.stderr == "after\n"  # quiet on a clean push


# ---- push-check -------------------------------------------------------------------------------


def test_git_pre_push_lines_are_read_and_deletes_told_apart():
    text = (
        f"refs/heads/main {SHA} refs/heads/main {ZERO}\n"
        f"(delete) {ZERO} refs/heads/old {SHA}\n"
        f"refs/tags/v1 {'b' * 64} refs/tags/v1 {'0' * 64}\n"  # SHA-256 repositories
        "not a ref line\n"
    )
    updates = parse_updates(text)
    assert [u.deletes for u in updates] == [False, True, False]
    assert updates[0] == RefUpdate("refs/heads/main", SHA, "refs/heads/main", ZERO)
    assert parse_updates("") == []


def test_push_check_never_blocks_a_delete(tmp_path, monkeypatch):
    for name in ("check_push", "check_pushed"):
        monkeypatch.setattr(protect, name, lambda *a: pytest.fail("checked a delete"))
    code, text = push_check(tmp_path, f"(delete) {ZERO} refs/heads/x {SHA}\n", {})
    assert (code, text) == (0, "agentprint: OK")


@pytest.mark.parametrize("error", [CannotCheck("git is not installed"), RuntimeError("bug")])
@pytest.mark.parametrize("refs", [f"refs/heads/main {SHA} refs/heads/main {ZERO}\n", ""])
def test_push_check_lets_the_push_go_when_it_cannot_check_unless_strict(
    tmp_path, monkeypatch, error, refs
):
    def boom(*args):
        raise error

    monkeypatch.setattr(protect, "check_push", boom)
    monkeypatch.setattr(protect, "check_pushed", boom)
    code, text = push_check(tmp_path, refs, {})
    assert code == 0 and "allowed" in text and not text.startswith("agentprint: BLOCK")
    for env, strict in (({}, True), ({"AGENTPRINT_STRICT": "1"}, False)):
        code, text = push_check(tmp_path, refs, env, strict=strict)
        assert code == 1 and text.startswith("agentprint: BLOCK (strict mode)")


def test_push_check_masks_keys_in_why_it_could_not_check(tmp_path, monkeypatch):
    def boom(cwd):
        raise CannotCheck(f"odd folder {fakes.GITHUB}")

    monkeypatch.setattr(protect, "check_push", boom)
    code, text = push_check(tmp_path, None, {})
    assert fakes.GITHUB not in text and fakes.GITHUB[:4] in text


def test_a_partly_read_push_is_said_and_blocks_only_in_strict_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(protect, "check_pushed", lambda cwd, updates: ([], ["read only part"]))
    refs = f"refs/heads/main {SHA} refs/heads/main {ZERO}\n"
    code, text = push_check(tmp_path, refs, {})
    assert code == 0 and "agentprint: read only part." in text
    code, text = push_check(tmp_path, refs, {}, strict=True)
    assert code == 1 and text.startswith("agentprint: BLOCK (strict mode)")


def _commit_leak(repo):
    leak = {"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}}
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(json.dumps(leak))
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
    return git(repo, "rev-parse", "HEAD").strip()


@needs_git
def test_push_check_command_blocks_a_leaky_commit(repo, monkeypatch):
    head = _commit_leak(repo)
    monkeypatch.chdir(repo)
    refs = f"refs/heads/main {head} refs/heads/main {ZERO}\n"
    result = CliRunner().invoke(app, ["push-check"], input=refs)
    assert result.exit_code == 1
    assert result.output.startswith("agentprint: BLOCK")
    assert fakes.ANTHROPIC not in result.output
    deleting = CliRunner().invoke(app, ["push-check"], input=f"(delete) {ZERO} refs/heads/x {head}")
    assert deleting.exit_code == 0


@needs_git
def test_push_check_reads_the_pushed_commit_not_the_working_tree(repo, monkeypatch):
    head = _commit_leak(repo)
    git(repo, "rm", "-q", "--cached", ".claude/settings.local.json")  # the index is clean now
    (repo / ".claude" / "settings.local.json").unlink()  # and so is the working tree
    monkeypatch.chdir(repo)
    assert push_check(repo, None, {})[0] == 0  # no ref lines: the working tree, clean
    code, text = push_check(repo, f"refs/heads/main {head} refs/heads/main {ZERO}\n", {})
    assert code == 1 and "AP002" in text and "AP006" in text


@needs_git
def test_a_key_only_in_an_older_pushed_commit_gets_the_rewrite_brief(repo):
    first = _commit_leak(repo)
    git(repo, "rm", "-q", "--cached", ".claude/settings.local.json")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "y")
    head = git(repo, "rev-parse", "HEAD").strip()
    code, text = push_check(repo, f"refs/heads/main {head} refs/heads/main {ZERO}\n", {})
    assert code == 1
    assert f"in pushed commit {first[:12]}" in text
    assert "git reset --soft" in text and "Rotate the Anthropic API key" in text
    assert 'starts "sk-a…(len 105)"' in text  # the key stub, without the commit note
    assert "AP006" not in text  # an older version only matters for its keys


def test_push_check_is_hidden_from_help():
    text = CliRunner().invoke(app, ["--help"]).output
    assert "push-check" not in text and "protect" in text


# ---- the CLI, status and scan -----------------------------------------------------------------


def test_cli_undo_takes_no_strict_or_global(tmp_path):
    for flag in ("--strict", "--global"):
        result = CliRunner().invoke(app, ["protect", "--undo", flag, str(tmp_path)])
        assert result.exit_code == 2


@needs_git
def test_cli_protect_and_undo(repo, home, monkeypatch):
    monkeypatch.setattr(protect, "hook_agentprint", lambda: ARGV)
    (repo / "package.json").write_text('{\n  "name": "x"\n}\n')
    result = CliRunner().invoke(app, ["protect", str(repo)])
    assert result.exit_code == 0, result.output
    assert "git: added" in result.output and "npm:" in result.output
    result = CliRunner().invoke(app, ["protect", "--undo", str(repo)])
    assert result.exit_code == 0, result.output
    assert (repo / "package.json").read_text() == '{\n  "name": "x"\n}\n'
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


@needs_git
def test_status_and_scan_say_when_the_repo_is_protected(repo, home, monkeypatch):
    monkeypatch.chdir(repo)
    status = ["status", "--home", str(home), "--format", "agent"]
    scan = ["scan", str(repo), "--no-home", "--format", "agent"]
    plain_status = CliRunner().invoke(app, status).output
    plain_scan = CliRunner().invoke(app, scan).output
    assert "protected" not in plain_status and "protected" not in plain_scan
    (repo / "package.json").write_text('{"name": "x"}\n')
    run(repo, home)
    line = "protected (agentprint protect): git pre-push hook, npm prepublishOnly"
    assert CliRunner().invoke(app, status).output == plain_status + line + "\n"
    assert CliRunner().invoke(app, scan).output == plain_scan + line + "\n"
    data = json.loads(CliRunner().invoke(app, [*status[:-1], "json"]).output)
    assert data["protected"] == ["git pre-push hook", "npm prepublishOnly"]
    table = CliRunner().invoke(app, [*status[:-1], "table", "--plain"]).output
    assert line in " ".join(table.split())
