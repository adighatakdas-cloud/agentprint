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
look for keys: AP001/AP002/AP005 already report any key in them. POSIX only: skipped on Windows,
where access is governed by ACLs.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..agents.base import HomeArtifact
from ..models import Artifact, Finding, Kind, Severity
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


def _is_windows() -> bool:
    return os.name == "nt"


def _parents(path: Path) -> list[Path]:
    return list(path.parents)


@dataclass(frozen=True)
class _Exposed:
    path: Path
    artifact: Artifact
    classes: tuple[str, ...]
    mode: int


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

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        if _is_windows():
            return []
        cache: dict[Path, os.stat_result | None] = {}
        groups: dict[tuple[Path, bool], list[_Exposed]] = {}
        for artifact in artifacts:
            if not _in_scope(artifact):
                continue
            folder = _data_folder(artifact)
            for path in _paths(artifact):
                exposure = _exposure(path, cache)
                if exposure is None:
                    continue
                key = (folder.absolute(), True) if folder else (path.absolute(), False)
                groups.setdefault(key, []).append(_Exposed(path, artifact, *exposure))
        return [
            self._finding(target, is_folder, items) for (target, is_folder), items in groups.items()
        ]

    def _finding(self, target: Path, is_folder: bool, items: list[_Exposed]) -> Finding:
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
        return Finding(
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
            detail=detail + f"(mode {first.mode:04o})",
            fix=command if _SAFE_PATH.fullmatch(str(target)) else plain,
            count=len(items),
        )


CHECK = ReadableByOthers()
