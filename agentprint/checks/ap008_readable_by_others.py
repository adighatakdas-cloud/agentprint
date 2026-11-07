"""AP008: agent data on this machine can be read by other local users.

History, settings and credential stores often hold keys and private conversations. Some agents
create them readable by everyone (Hermes CVE-2026-53870: world-readable history and webhook
secrets).

What is checked: files from the home folder (HomeArtifact), plus personal and credential files
inside projects. Shared project config (.mcp.json, .claude/settings.json, .github/hooks/...) is
not: git doesn't record read permissions, and teammates are meant to read it.

When a file counts as readable: symlinks are resolved, then the file's mode must allow the class
AND every folder above the real file must let that class in. A member of the file's group uses
a folder's group bits when the folder has the same group, and its other bits otherwise (other
group memberships are not resolved). Each folder is looked at once per scan.

The fix is one short command: `chmod -R go-rwx <agent folder>` (e.g. ~/.hermes, which also
protects files created later) or `chmod go-rwx <file>`. go-rwx, never 600: it keeps the
owner's execute bit, so hook scripts still run. A path is put in a command only when every
character is in [A-Za-z0-9._@%+=:,/-]; otherwise the fix is a plain sentence that points at the
finding's location, so no file name can ever become shell syntax.

Severity (Ruling): high for credential stores, medium otherwise. Files are not read again to
look for keys: AP001/AP002/AP005 already report any key in them.

On Windows there are no mode bits: the file's access list (ACL) decides, read by winacl (which
says exactly when it counts). The owner is the file's owner SID against this process's user
(an elevated process's files are owned by Administrators, which counts as this user). The fix
is the icacls recipe for one file, or `agentprint fix` for an agent folder with several (it runs
icacls on each file), never chmod. A file whose list can't be read or holds entries winacl does
not read is not reported: the sweep says it could not check it (platform.scan_notes()).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .. import platform, winacl
from ..agents.base import HomeArtifact
from ..models import Artifact, Finding, Kind, Severity
from ..sanitize import quote
from ..sqlite_text import is_sqlite
from .base import safe_snippet

_LABEL = {
    Kind.CREDENTIALS: "credential store",
    Kind.HISTORY: "history file",
    Kind.SETTINGS: "settings file",
    Kind.PERSONAL: "personal file",
}
_SQLITE_SIDECARS = ("-wal", "-journal")  # recent rows live here until checkpointed
_SAFE_PATH = re.compile(r"[A-Za-z0-9._@%+=:,/-]+")
# How many path parts under home make up one agent's data folder: ~/.hermes,
# ~/.config/opencode, ~/.local/share/opencode, ~/Library/Application Support/Cursor.
_DEPTH = {".config": 2, ".local": 3, "Library": 3, "AppData": 3}
_PLAIN_FOLDER = (
    "Make the folder named in the location of this finding private to you: run "
    "chmod -R go-rwx on it, passing the path in single quotes."
)
_PLAIN_FILE = (
    "Make the file named in the location of this finding private to you: run chmod go-rwx on "
    "it, passing the path in single quotes."
)
# Windows: a path goes into the icacls command only when every character is safe inside double
# quotes for both PowerShell and cmd (no quote, $, backtick, % or !).
_WIN_SAFE_PATH = re.compile(r"[A-Za-z0-9 ._@+=:,\\/~()-]+")
_WIN_PLAIN_FILE = (
    "Make the file named in the location of this finding private to you: run icacls on it, "
    "passing the path in double quotes, with {args}."
)
_WIN_FOLDER = (
    "Make the files here private to you: run agentprint fix --only AP008. It runs {recipe} on "
    "each one, and agentprint fix --undo puts them back."
)


def _is_windows() -> bool:
    return platform.is_windows()


def _parents(path: Path) -> list[Path]:
    return list(path.parents)


@dataclass(frozen=True)
class _Exposed:
    path: Path
    artifact: Artifact
    classes: tuple[str, ...]
    how: str  # "mode 0644"; on Windows "access list: Everyone, Users"
    remove: tuple[str, ...] = ()  # Windows: other SIDs the file's own entries let read it


def _folder_stat(folder: Path, cache: dict[Path, os.stat_result | None]) -> os.stat_result | None:
    if folder not in cache:
        try:
            cache[folder] = folder.stat()
        except OSError:
            cache[folder] = None
    return cache[folder]


def _exposure(
    path: Path, cache: dict[Path, os.stat_result | None]
) -> tuple[tuple[str, ...], int] | None:
    """(who besides the owner can read the file, its mode), or None if nobody else can."""
    try:
        real = path.resolve(strict=True)
        st = real.stat()
    except (OSError, RuntimeError, ValueError):
        return None  # missing, vanished, dangling or looping symlink
    if not stat.S_ISREG(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        return None  # someone else's file is theirs to fix
    group = bool(st.st_mode & stat.S_IRGRP)
    others = bool(st.st_mode & stat.S_IROTH)
    for parent in _parents(real):
        if not (group or others):
            break
        folder = _folder_stat(parent, cache)
        if folder is None:
            return None
        group_bit = stat.S_IXGRP if folder.st_gid == st.st_gid else stat.S_IXOTH
        group = group and bool(folder.st_mode & group_bit)
        others = others and bool(folder.st_mode & stat.S_IXOTH)
    classes = tuple(who for who, can in (("group", group), ("others", others)) if can)
    return (classes, stat.S_IMODE(st.st_mode)) if classes else None


def _data_folder(artifact: Artifact) -> Path | None:
    """The agent's own folder under home (~/.hermes), or None for a file outside one."""
    if not isinstance(artifact, HomeArtifact) or artifact.home is None:
        return None
    try:
        parts = artifact.path.relative_to(artifact.home).parts
    except ValueError:
        return None
    if not parts:
        return None
    depth = _DEPTH.get(parts[0], 1 if parts[0].startswith(".") else 0)
    if depth == 0 or len(parts) <= depth:
        return None
    return artifact.home.joinpath(*parts[:depth])


def _in_scope(artifact: Artifact) -> bool:
    return (
        isinstance(artifact, HomeArtifact) or artifact.personal or artifact.kind is Kind.CREDENTIALS
    )


def _paths(artifact: Artifact) -> list[Path]:
    paths = [artifact.path]
    if is_sqlite(artifact.path):
        paths += [artifact.path.with_name(artifact.path.name + s) for s in _SQLITE_SIDECARS]
    return paths


def exposed_paths(
    artifact: Artifact, cache: dict[Path, os.stat_result | None] | None = None
) -> list[Path]:
    """The files of this artifact (and its SQLite sidecars) that others can read: the exact list
    the check reports, which `agentprint fix` locks."""
    if not _in_scope(artifact):
        return []
    if _is_windows():
        user = _windows_user()
        return [p for p in _paths(artifact) if _windows_exposure(p, user, []) is not None]
    cache = {} if cache is None else cache
    return [path for path in _paths(artifact) if _exposure(path, cache) is not None]


# ---- Windows: the file's access list --------------------------------------------------------

_User = tuple[str, bool] | None  # (this process's user SID, elevated); None: unknown


def _windows_user() -> _User:
    try:
        return winacl.current_user()
    except (OSError, ValueError):
        return None


def _windows_exposure(
    path: Path, user: _User, unchecked: list[tuple[Path, str]]
) -> tuple[tuple[str, ...], str, tuple[str, ...]] | None:
    """(("others",), how, the SIDs its own entries let read it) when other users can read the
    file; None when nobody else can, it is someone else's, or it could not be checked (then it
    goes on `unchecked` with the reason)."""
    try:
        real = path.resolve(strict=True)
        if not stat.S_ISREG(real.stat().st_mode):
            return None
    except (OSError, RuntimeError, ValueError):
        return None  # missing, vanished, dangling or looping link
    access = winacl.exposure(str(real), *user) if user else winacl.Access(unknown=winacl.NO_USER)
    if access is not None and access.unknown:
        unchecked.append((path, access.unknown))
        return None
    if access is None or not access.readers:
        return None
    how = "access list: " + ", ".join(winacl.name(sid) for sid in access.readers)
    return ("others",), how, access.explicit


def _unchecked_note(unchecked: list[tuple[Path, str]]) -> str | None:
    """The sweep's "not checked" line for files whose access list could not be judged."""
    if not unchecked:
        return None
    paths = list(dict.fromkeys(path for path, _ in unchecked))
    path, why = unchecked[0]
    one = len(paths) == 1
    files, first, them = (
        ("1 agent file", "", "it") if one else (f"{len(paths)} agent files", ", the first", "them")
    )
    return (
        f"AP008 could not check who can read {files} on Windows ({quote(str(path))}{first}: "
        f'{why}). Check {them} with icacls "<file>" and tell the human if anyone else can read '
        f"{them}."
    )


def _windows_fix(target: Path, is_folder: bool, items: list[_Exposed], me: str) -> str:
    """The icacls recipe for one file (the only file of an agent folder too), or agentprint fix
    for several: there is no folder-wide icacls equivalent of chmod -R go-rwx that also drops
    what each file grants on its own."""
    first = items[0]
    if is_folder:
        path = first.path.absolute()
        if len(items) == 1 and _WIN_SAFE_PATH.fullmatch(str(path)):
            return winacl.recipe(str(path), me, first.remove)
        return _WIN_FOLDER.format(recipe=winacl.recipe("<file>", me))
    if _WIN_SAFE_PATH.fullmatch(str(target)):
        return winacl.recipe(str(target), me, first.remove)
    return _WIN_PLAIN_FILE.format(args=" ".join(winacl.lock_args("", me, first.remove)[1:]))


def _who(items: list[_Exposed]) -> str:
    found = {c for item in items for c in item.classes}
    if found == {"group", "others"}:
        return "its group and other users"
    return "its group" if found == {"group"} else "other users"


class ReadableByOthers:
    id = "AP008"
    severity = Severity.MEDIUM  # high for credential stores
    title = "Agent data readable by other users"
    fix = (
        "Remove group and other access: chmod -R go-rwx on the agent's folder, or chmod go-rwx "
        "on a single file."
    )
    windows_fix = (  # what `explain` says on Windows
        'Make the files private to you: agentprint fix runs icacls "<file>" /inheritance:r '
        "/grant:r *<your SID>:F *S-1-5-18:F *S-1-5-32-544:F on each one."
    )

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        if _is_windows():
            return self._run_windows(artifacts)
        platform.set_run_note(self.id, None)
        cache: dict[Path, os.stat_result | None] = {}
        groups: dict[tuple[Path, bool], list[_Exposed]] = {}
        for artifact in artifacts:
            if not _in_scope(artifact):
                continue
            folder = _data_folder(artifact)
            for path in exposed_paths(artifact, cache):
                exposure = _exposure(path, cache)
                if exposure is None:  # changed since the line above
                    continue
                key = (folder.absolute(), True) if folder else (path.absolute(), False)
                classes, mode = exposure
                groups.setdefault(key, []).append(
                    _Exposed(path, artifact, classes, f"mode {mode:04o}")
                )
        return [
            self._finding(target, is_folder, items) for (target, is_folder), items in groups.items()
        ]

    def _run_windows(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        user = _windows_user()
        unchecked: list[tuple[Path, str]] = []
        groups: dict[tuple[Path, bool], list[_Exposed]] = {}
        for artifact in artifacts:
            if not _in_scope(artifact):
                continue
            folder = _data_folder(artifact)
            for path in _paths(artifact):
                exposure = _windows_exposure(path, user, unchecked)
                if exposure is None:
                    continue
                key = (folder.absolute(), True) if folder else (path.absolute(), False)
                groups.setdefault(key, []).append(_Exposed(path, artifact, *exposure))
        platform.set_run_note(self.id, _unchecked_note(unchecked))
        me = user[0] if user else ""
        return [
            self._finding(target, is_folder, items, me)
            for (target, is_folder), items in groups.items()
        ]

    def _finding(
        self, target: Path, is_folder: bool, items: list[_Exposed], me: str | None = None
    ) -> Finding:
        """One print; `me` (this user's SID) is set on Windows, where the fix is icacls."""
        first = items[0]
        who = _who(items)
        label = _LABEL.get(first.artifact.kind, "agent file")
        name = safe_snippet(first.path.name, 60)
        if is_folder:
            # target is absolute, so it can't be read as an option; BSD chmod rejects a `--`
            # placed after the mode.
            command, plain = f"chmod -R go-rwx {target}", _PLAIN_FOLDER
            if len(items) == 1:
                detail = f"{label} {name} in this agent folder is readable by {who} "
            else:
                detail = f"{len(items)} agent files here are readable by {who}, e.g. {name} "
        else:
            command, plain = f"chmod go-rwx {target}", _PLAIN_FILE
            detail = f"{label} is readable by {who} "
        if me is None:
            fix = command if _SAFE_PATH.fullmatch(str(target)) else plain
        else:
            fix = _windows_fix(target, is_folder, items, me)
        finding = Finding(
            check_id=self.id,
            severity=(
                Severity.HIGH
                if any(item.artifact.kind is Kind.CREDENTIALS for item in items)
                else Severity.MEDIUM
            ),
            title=self.title,
            agent=first.artifact.agent,
            location=str(target),
            line=None,
            detail=detail + f"({first.how})",
            fix=fix,
            count=len(items),
        )
        finding.paths = tuple(item.path for item in items)
        return finding


CHECK = ReadableByOthers()
