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
MAX_COMMITS = 2000  # commits read per pushed ref; past that, the newest ones (and it says so)
MAX_SETTINGS_BYTES = 1024 * 1024  # a settings file bigger than this is not a settings file
MAX_READ_BYTES = 64 * 1024 * 1024  # all file contents read for one push
# Where agent and settings files live (every agent module's discover_project looks only at
# files at the top of the project or inside a top-level dot-folder), and the kinds of files
# they are: text config, never a build artifact or an archive (.yarn/cache is full of zips).
_TEXT = {"", ".json", ".jsonc", ".json5", ".toml", ".yaml", ".yml", ".md", ".mdc", ".txt"}
_TEXT |= {".ini", ".cfg", ".conf", ".local", ".env"}
_REGULAR = {"100644", "100755"}


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


def _wanted(name: str) -> bool:
    """A file a check reads: an agent file anywhere, or a text file at the top of the project or
    in a top-level dot-folder (where every agent keeps its project settings)."""
    if is_agent_file(name):
        return True
    top, _, rest = name.partition("/")
    if rest and not top.startswith("."):
        return False
    base = name.rpartition("/")[2]
    return base.startswith(".env") or os.path.splitext(base)[1].lower() in _TEXT


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
        if len(parts) != 4 or parts[1] != "blob" or not _wanted(name):
            continue
        mode, _, blob, size = parts
        entries[name] = _Entry(blob, int(size) if size.isdigit() else None, mode in _REGULAR)
    return entries


def _pushed_commits(root: Path, update: RefUpdate) -> tuple[list[str], bool]:
    """The commits this ref update would upload (none on a remote-tracking ref yet, nor in the
    remote's old commit), newest first, and whether there were more than MAX_COMMITS."""
    if _git(root, "cat-file", "-e", f"{update.local_sha}^{{commit}}") is None:
        return [], False  # a tag on a tree or a blob: only its tree is read
    args = ["rev-list", f"--max-count={MAX_COMMITS + 1}", update.local_sha, "--not", "--remotes"]
    old = update.remote_sha
    if not _ZERO.fullmatch(old) and _git(root, "cat-file", "-e", f"{old}^{{commit}}") is not None:
        args.append(old)
    commits = _git_bytes(root, *args).decode("ascii", "replace").split()
    return commits[:MAX_COMMITS], len(commits) > MAX_COMMITS


def _touched(root: Path, commits: list[str]) -> list[tuple[str, dict[str, _Entry]]]:
    """For each commit, newest first, the wanted files it added or changed (their new blobs)."""
    if not commits:
        return []
    out = _git_bytes(
        root,
        "diff-tree",
        "--stdin",
        "-r",
        "-z",
        "--root",
        "--no-renames",
        "--raw",
        stdin="\n".join(commits).encode() + b"\n",
    )
    result: list[tuple[str, dict[str, _Entry]]] = []
    tokens = iter(out.decode("utf-8", "replace").split("\0"))
    for token in tokens:
        if _HEX.fullmatch(token):
            result.append((token, {}))
        elif token.startswith(":") and result:
            name = next(tokens, "")
            parts = token[1:].split()
            if len(parts) == 5 and parts[4][:1] in "AMT" and _wanted(name):
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
    def __init__(self) -> None:
        self.left = MAX_READ_BYTES

    def take(self, size: int) -> None:
        self.left -= size
        if self.left < 0:
            raise CannotCheck(
                "this push holds more agent and settings files than agentprint reads in one go"
            )


def _snapshot(root: Path, entries: dict[str, _Entry], budget: _Budget) -> list[Finding]:
    """The working-tree checks, run on these files as git stores them (written to a private
    temporary folder, then reported at their place in the repository)."""
    _sizes(root, entries.values())
    readable = {}
    for name, entry in entries.items():
        limit = MAX_CONTENT_BYTES if is_agent_file(name) else MAX_SETTINGS_BYTES
        if entry.regular and entry.size is not None and entry.size <= limit:
            readable[name] = entry
    for entry in readable.values():
        budget.take(entry.size or 0)
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


def check_pushed(cwd: Path, updates: list[RefUpdate]) -> tuple[list[Finding], list[str]]:
    """Findings for what these ref updates would upload, read from git's objects (never the
    working tree), and notes on what was not read. Deletes upload nothing. Raises CannotCheck
    when git can't read them."""
    if shutil.which("git") is None:
        raise CannotCheck("git is not installed, so the repository can't be read")
    root = repo_root(cwd) or cwd
    budget = _Budget()
    findings: list[Finding] = []
    notes: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for update in updates:
        if update.deletes:
            continue
        tip = _tree(root, update.local_sha)
        for finding in _snapshot(root, tip, budget):
            if _key(finding) not in seen:
                seen.add(_key(finding))
                findings.append(finding)
        commits, more = _pushed_commits(root, update)
        if more:
            notes.append(
                f"{clean(update.local_ref)} uploads more than {MAX_COMMITS} commits; only the "
                f"newest {MAX_COMMITS} were read for keys added and removed on the way"
            )
        for commit, files in _touched(root, commits):
            files = {
                name: e for name, e in files.items() if name not in tip or tip[name].blob != e.blob
            }
            for finding in _snapshot(root, files, budget) if files else []:
                if finding.check_id not in (AP002.id, AP005.id) or _key(finding) in seen:
                    continue  # older versions only matter for the keys in them
                seen.add(_key(finding))
                finding.detail += f"{IN_PUSHED_COMMIT}{commit[:12]})"
                findings.append(finding)
    findings.sort(key=lambda f: (-f.severity, f.check_id, f.location, f.line or 0))
    return findings, notes
