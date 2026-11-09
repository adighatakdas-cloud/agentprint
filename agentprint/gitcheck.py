"""What a `git push` would publish: agent files tracked by git, and keys inside them.

Two views. `check_push` reads the working tree: what git tracks now (the agent hook uses it
before `git push` runs, when the refs are not known yet). `check_pushed` reads git's own objects
for the refs a push names (the pre-push hook gets them on stdin): the tree each ref points at,
plus keys in agent and settings files changed by the commits the push would upload, so a key
added and removed again on the way out is still found. It never reads the working tree.

Read-only and offline: only local git commands (`ls-files`, `rev-parse`, `ls-tree`,
`rev-list`, `diff-tree`, `cat-file`) are run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import platform
from .agents import ALL_AGENTS
from .checks.ap002_settings_secret import CHECK as AP002
from .checks.ap005_mcp_inline_secret import CHECK as AP005
from .checks.ap006_package_leak import check_listing, is_agent_file
from .checks.ap007_local_not_ignored import LocalFileNotIgnored as AP007
from .models import IN_PUSHED_COMMIT, CannotCheck, Finding, Kind
from .pack import MAX_CONTENT_BYTES, PackageListing
from .sanitize import clean
from .scanner import drop_claimed

GIT_TIMEOUT = 20
PUSHED_TITLE = "Agent file would be pushed to the git remote"
PUSHED_FIX = "Untrack it with `git rm --cached`, add it to .gitignore, commit, then push."


def _git(root: Path, *args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "-C", str(root), "-c", "core.quotepath=false", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT,
            **platform.subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def repo_root(cwd: Path) -> Path | None:
    """The repository's top folder, spelled relative to `cwd` (so /var vs /private/var and
    symlinked checkouts keep the caller's spelling)."""
    out = _git(cwd, "rev-parse", "--show-cdup")
    if out is None:
        return None
    return Path(os.path.normpath(cwd / out.strip())) if out.strip() else cwd


def _read(path: Path) -> bytes:
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_CONTENT_BYTES:
            return path.read_bytes()
    except OSError:
        pass
    return b""


def check_push(cwd: Path) -> list[Finding]:
    """Findings for what a push from `cwd` would publish. Raises CannotCheck when it can't
    look (no such folder, git missing); a folder outside any repository has nothing to push."""
    if not cwd.is_dir():
        raise CannotCheck(f"the folder {clean(str(cwd))!r} does not exist")
    if shutil.which("git") is None:
        raise CannotCheck("git is not installed, so the repository can't be read")
    root = repo_root(cwd)
    if root is None:
        return []
    tracked_out = _git(root, "ls-files", "-z")
    tracked = [name for name in (tracked_out or "").split("\0") if name]
    findings = _tracked_agent_files(root, tracked)
    findings += _tracked_settings_secrets(root, set(tracked), findings)
    findings += _not_ignored(root)
    return sorted(findings, key=lambda f: (-f.severity, f.check_id, f.location, f.line or 0))


def _tracked_agent_files(
    root: Path,
    tracked: list[str],
    contents: dict[str, bytes] | None = None,
    skipped: Iterable[str] = (),
) -> list[Finding]:
    """AP006 (and AP002 for keys inside) for each agent file in `tracked`. Contents are read
    from `root` unless given."""
    agent_files = [name for name in tracked if is_agent_file(name)]
    listing = PackageListing(
        label="git",
        files=agent_files,
        contents={
            name: (_read(root / name) if contents is None else contents.get(name, b""))
            for name in agent_files
        },
        skipped=[name for name in skipped if name in agent_files],
    )
    findings = check_listing(listing)
    for finding in findings:
        finding.location = str(root / finding.location.partition("!")[2])
        if finding.check_id != AP002.id:
            finding.title, finding.fix = PUSHED_TITLE, PUSHED_FIX
    return findings


def _tracked_settings_secrets(
    root: Path, tracked: set[str], already: list[Finding]
) -> list[Finding]:
    """Keys in tracked shared agent settings (.mcp.json, .claude/settings.json, ...), reported
    exactly as a scan reports them (an MCP-config key is AP005, other keys AP002)."""
    seen = {f.location for f in already}
    artifacts = [
        artifact
        for agent in ALL_AGENTS
        for artifact in agent.discover_project(root)
        if artifact.kind is Kind.SETTINGS
        and str(artifact.path) not in seen
        and artifact.path.relative_to(root).as_posix() in tracked
    ]
    return drop_claimed([*AP002.run(artifacts), *AP005.run(artifacts)])


def _not_ignored(root: Path) -> list[Finding]:
    out = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    return [
        # Same problem the AP007 check reports in a scan, so it carries that check's metadata.
        Finding(
            AP007.id,
            AP007.severity,
            AP007.title,
            "agent",
            str(root / name),
            None,
            "Agent file that is neither committed nor ignored; `git add .` would commit it.",
            AP007.fix,
        )
        for name in (out or "").split("\0")
        if name and is_agent_file(name)
    ]


# ---- what a push uploads: git's pre-push lines --------------------------------------------------

_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_ZERO = re.compile(r"0{40}|0{64}")
MAX_COMMITS = 10_000  # commits read per pushed ref; past that, "could not check" (never OK)
DIFF_BATCH = 1000  # commits per `git diff-tree --stdin` run, so its output stays small
MAX_SETTINGS_BYTES = 1024 * 1024  # a settings file bigger than this is not a settings file
MAX_READ_BYTES = 64 * 1024 * 1024  # all file contents read for one push
_REGULAR = {"100644", "100755"}
_GLOB = re.compile(r"[*?\[]")


@dataclass(frozen=True)
class RefUpdate:
    """One line of git's pre-push input: `<local ref> <local sha> <remote ref> <remote sha>`."""

    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    @property
    def deletes(self) -> bool:
        """Deleting a remote ref (an all-zero local sha) uploads nothing."""
        return bool(_ZERO.fullmatch(self.local_sha))


def parse_updates(text: str) -> list[RefUpdate]:
    """git's pre-push lines; anything else is left out."""
    updates = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and _HEX.fullmatch(parts[1]) and _HEX.fullmatch(parts[3]):
            updates.append(RefUpdate(*parts))
    return updates


def _near_top(name: str) -> bool:
    """A file a check might read: an agent file anywhere, or a file at the top of the project or
    inside a top-level dot-folder (the only places any agent module's discover_project looks)."""
    if is_agent_file(name):
        return True
    top, _, rest = name.partition("/")
    return not rest or top.startswith(".")


def _read_by_checks(names: Iterable[str]) -> set[str]:
    """Which of these repository paths a check reads: agent files (AP006, and AP002 for keys in
    them), and the settings files each agent module's discover_project finds (AP002, AP005).
    Discovery runs over empty stand-ins for the names, so nothing is read to decide; a
    lockfile or a README is never read."""
    names = set(names)
    agent = {name for name in names if is_agent_file(name)}
    rest = names - agent
    if not rest:
        return agent
    found: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="agentprint-names-") as tmp:
        base = Path(tmp)
        for name in rest:
            parts = name.split("/")
            if any(part in ("", ".", "..") for part in parts):
                continue
            try:
                target = base.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            except OSError:  # a file and a folder of the same name, or a name the OS refuses
                continue
        for agent_module in ALL_AGENTS:
            try:
                artifacts = list(agent_module.discover_project(base))
            except Exception:  # noqa: BLE001 - one agent module's bug must not stop the others
                continue
            for artifact in artifacts:
                if artifact.kind is Kind.SETTINGS:
                    found.add(artifact.path.relative_to(base).as_posix())
    return agent | (found & rest)


def _git_bytes(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise CannotCheck("git is not installed, so the repository can't be read")
    try:
        result = subprocess.run(
            [git, "-C", str(root), "-c", "core.quotepath=false", *args],
            input=stdin,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT * 3,
            **platform.subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CannotCheck(f"git {args[0]} did not finish ({type(exc).__name__})") from exc
    if result.returncode != 0:
        why = result.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        raise CannotCheck(f"git {args[0]} failed: {clean(why[0] if why else 'no reason given')}")
    return result.stdout


@dataclass
class _Entry:
    blob: str
    size: int | None  # None: not known yet
    regular: bool  # a plain file (not a link or a submodule)


def _tree(root: Path, sha: str) -> dict[str, _Entry]:
    """Every file the commit (or tag) `sha` points at that a check might read."""
    out = _git_bytes(root, "ls-tree", "-r", "-z", "--long", "--full-tree", sha)
    entries: dict[str, _Entry] = {}
    for record in out.decode("utf-8", "replace").split("\0"):
        meta, _, name = record.partition("\t")
        parts = meta.split()
        if len(parts) != 4 or parts[1] != "blob" or not _near_top(name):
            continue
        mode, _, blob, size = parts
        entries[name] = _Entry(blob, int(size) if size.isdigit() else None, mode in _REGULAR)
    return entries


def _ref_tree(root: Path, update: RefUpdate) -> dict[str, _Entry]:
    """`_tree` for the object a ref update points at; a tag on a single file can't be placed."""
    try:
        return _tree(root, update.local_sha)
    except CannotCheck:
        if (_git(root, "cat-file", "-t", f"{update.local_sha}^{{}}") or "").strip() == "blob":
            raise CannotCheck(
                f"{clean(update.local_ref)} points at a single file, not a commit, so agentprint "
                "can't tell which file it is"
            ) from None
        raise


def _named_remote(root: Path, remote: str | None) -> bool:
    """A remote configured by that name (a push to a URL names none)."""
    if not remote or _GLOB.search(remote) or remote.startswith("-"):
        return False
    return _git(root, "config", "--get", f"remote.{remote}.url") is not None


def _pushed_commits(root: Path, update: RefUpdate, remote: str | None) -> tuple[list[str], bool]:
    """The commits this ref update would upload, newest first, and whether there were more than
    MAX_COMMITS. Left out: the remote's old commit for this ref, and what the remote-tracking
    refs of the remote being pushed to already hold (`--remotes=<name>`). Never what other
    remotes hold: a private mirror having a commit doesn't make it public."""
    if _git(root, "cat-file", "-e", f"{update.local_sha}^{{commit}}") is None:
        return [], False  # a tag on a tree: only its tree is read
    known = [f"--remotes={remote}"] if remote and _named_remote(root, remote) else []
    old = update.remote_sha
    if not _ZERO.fullmatch(old) and _git(root, "cat-file", "-e", f"{old}^{{commit}}") is not None:
        known.append(old)
    args = ["rev-list", f"--max-count={MAX_COMMITS + 1}", update.local_sha]
    commits = _git_bytes(root, *args, *(["--not", *known] if known else [])).decode().split()
    return commits[:MAX_COMMITS], len(commits) > MAX_COMMITS


def _touched(root: Path, commits: list[str]) -> list[tuple[str, dict[str, _Entry]]]:
    """For each commit, newest first, the files near the top it added or changed (their new
    blobs). A merge is compared with each of its parents (-m), so a file a merge brings in or
    changes on its own is read too."""
    result: list[tuple[str, dict[str, _Entry]]] = []
    for start in range(0, len(commits), DIFF_BATCH):
        batch = commits[start : start + DIFF_BATCH]
        out = _git_bytes(
            root,
            "diff-tree",
            "--stdin",
            "-r",
            "-m",
            "-z",
            "--root",
            "--no-renames",
            "--raw",
            stdin="\n".join(batch).encode() + b"\n",
        )
        tokens = iter(out.decode("utf-8", "replace").split("\0"))
        for token in tokens:
            if _HEX.fullmatch(token):
                result.append((token, {}))
            elif token.startswith(":") and result:
                name = next(tokens, "")
                parts = token[1:].split()
                if len(parts) == 5 and parts[4][:1] in "AMT" and _near_top(name):
                    result[-1][1][name] = _Entry(parts[3], None, parts[1] in _REGULAR)
    return [(commit, files) for commit, files in result if files]


def _sizes(root: Path, entries: Iterable[_Entry]) -> None:
    """Fill in each entry's size with one `git cat-file --batch-check`."""
    unknown = sorted({e.blob for e in entries if e.size is None})
    if not unknown:
        return
    out = _git_bytes(root, "cat-file", "--batch-check", stdin="\n".join(unknown).encode() + b"\n")
    sizes = {}
    for line in out.decode("ascii", "replace").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].isdigit():
            sizes[parts[0]] = int(parts[2])
    for entry in entries:
        if entry.size is None:
            entry.size = sizes.get(entry.blob)


def _contents(root: Path, blobs: list[str]) -> dict[str, bytes]:
    """The bytes of each blob, read with one `git cat-file --batch`."""
    if not blobs:
        return {}
    out = _git_bytes(root, "cat-file", "--batch", stdin="\n".join(blobs).encode() + b"\n")
    found: dict[str, bytes] = {}
    at = 0
    while at < len(out):
        end = out.index(b"\n", at)
        header = out[at:end].decode("ascii", "replace").split()
        at = end + 1
        if len(header) != 3 or not header[2].isdigit():
            continue  # "<sha> missing"
        size = int(header[2])
        found[header[0]] = out[at : at + size]
        at += size + 1
    return found


class _Budget:
    """File contents left to read for this push. Past it, files are listed but not read."""

    def __init__(self) -> None:
        self.left = MAX_READ_BYTES
        self.ran_out = False

    def take(self, size: int) -> bool:
        if size > self.left:
            self.ran_out = True
            return False
        self.left -= size
        return True


def _snapshot(root: Path, entries: dict[str, _Entry], budget: _Budget) -> list[Finding]:
    """The working-tree checks, run on these files as git stores them (written to a private
    temporary folder, then reported at their place in the repository). Only files a check
    reads are read; past the budget a file is listed (AP006 still applies) but not read."""
    entries = {name: e for name, e in entries.items() if name in _read_by_checks(entries)}
    if not entries:
        return []
    _sizes(root, entries.values())
    readable = {}
    for name, entry in entries.items():
        limit = MAX_CONTENT_BYTES if is_agent_file(name) else MAX_SETTINGS_BYTES
        fits = entry.regular and entry.size is not None and entry.size <= limit
        if fits and budget.take(entry.size or 0):
            readable[name] = entry
    data = _contents(root, sorted({e.blob for e in readable.values()}))
    contents = {name: data.get(e.blob, b"") for name, e in readable.items()}
    skipped = [name for name in entries if name not in readable]
    with tempfile.TemporaryDirectory(prefix="agentprint-push-") as tmp:
        base = Path(tmp)
        written = []
        for name, blob in contents.items():
            parts = name.split("/")
            if any(part in ("", ".", "..") for part in parts):
                continue
            target = base.joinpath(*parts)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
            except OSError:  # a name this system can't hold (say "a:b" on Windows)
                skipped.append(name)
                continue
            written.append(name)
        findings = _tracked_agent_files(base, sorted(entries), contents, skipped)
        findings += _tracked_settings_secrets(base, set(written), findings)
        for finding in findings:
            rel = Path(finding.location).relative_to(base)
            finding.location = str(root / rel)
    return findings


def _key(finding: Finding) -> tuple[str, str, str]:
    return finding.check_id, finding.location, finding.detail.partition(IN_PUSHED_COMMIT)[0]


def check_pushed(
    cwd: Path, updates: list[RefUpdate], remote: str | None = None
) -> tuple[list[Finding], list[str]]:
    """Findings for what these ref updates would upload, read from git's objects (never the
    working tree), and what could not be checked. `remote` is the name git's pre-push hook got
    (a push to a URL names none). Deletes upload nothing. Raises CannotCheck only when git is
    missing; anything else that can't be read is a note, and never takes back a finding
    already made (Ruling X19)."""
    if shutil.which("git") is None:
        raise CannotCheck("git is not installed, so the repository can't be read")
    root = repo_root(cwd) or cwd
    budget = _Budget()
    findings: list[Finding] = []
    notes: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    read: set[tuple[str, str]] = set()  # (path, blob) already checked

    def add(found: list[Finding], commit: str | None = None) -> None:
        for finding in found:
            if commit is not None and finding.check_id not in (AP002.id, AP005.id):
                continue  # older versions only matter for the keys in them
            if _key(finding) in seen:
                continue
            seen.add(_key(finding))
            if commit is not None:
                finding.detail += f"{IN_PUSHED_COMMIT}{commit[:12]})"
            findings.append(finding)

    for update in updates:
        if update.deletes:
            continue
        ref = clean(update.local_ref)
        try:
            tip = _ref_tree(root, update)
            add(_snapshot(root, tip, budget))
            read.update((name, e.blob) for name, e in tip.items())
            commits, more = _pushed_commits(root, update, remote)
            if more:
                notes.append(
                    f"{ref} uploads more than {MAX_COMMITS} commits, so the older ones were not "
                    "read for keys added and removed on the way"
                )
            touched = _touched(root, commits)
            wanted = _read_by_checks(name for _, files in touched for name in files)
            for commit, files in touched:
                files = {n: e for n, e in files.items() if n in wanted and (n, e.blob) not in read}
                read.update((name, e.blob) for name, e in files.items())
                if files:
                    add(_snapshot(root, files, budget), commit)
        except CannotCheck as exc:
            notes.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - a bug in one ref keeps what was found
            notes.append(f"{ref}: agentprint hit an internal error ({type(exc).__name__})")
    if budget.ran_out:
        notes.append(
            "this push holds more agent and settings files than agentprint reads in one go, so "
            "some were not read"
        )
    findings.sort(key=lambda f: (-f.severity, f.check_id, f.location, f.line or 0))
    return findings, notes
