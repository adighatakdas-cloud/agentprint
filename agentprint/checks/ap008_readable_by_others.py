"""AP008: agent data on this machine can be read by other local users.

History, settings and credential stores often hold keys and private conversations. Some agents
create them readable by everyone (Hermes CVE-2026-53870: world-readable history and webhook
secrets). A file counts as readable by its group or by other users only when its mode allows it
AND every folder above it can be entered by that class, so a 0644 file inside a 0700 folder is
not reported. Group membership is not resolved: group bits are taken at face value.

Severity is high for a credential store or a file that holds a key, medium otherwise. Several
exposed files in one folder are one finding. POSIX only: skipped on Windows, where access is
governed by ACLs.
"""

from __future__ import annotations

import os
import shlex
import stat
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from ..files import iter_lines
from ..models import Artifact, Finding, Kind, Severity
from ..secrets import find_secrets
from ..sqlite_text import is_sqlite
from .base import safe_snippet

_LABEL = {
    Kind.CREDENTIALS: "credential store",
    Kind.HISTORY: "history file",
    Kind.SETTINGS: "settings file",
    Kind.PERSONAL: "personal file",
}
_LISTED = 5  # up to this many files, the fix names each one
_SQLITE_SIDECARS = ("-wal", "-journal")  # recent rows live here until checkpointed


def _is_windows() -> bool:
    return os.name == "nt"


def _parents(path: Path) -> list[Path]:
    return list(path.absolute().parents)


def readers(path: Path) -> tuple[str, ...]:
    """Who besides the owner can read the file: some of ("group", "others"), or ()."""
    try:
        st = path.stat()
    except OSError:
        return ()
    if not stat.S_ISREG(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        return ()  # someone else's file is theirs to fix
    group = bool(st.st_mode & stat.S_IRGRP)
    others = bool(st.st_mode & stat.S_IROTH)
    for parent in _parents(path):
        if not (group or others):
            break
        try:
            mode = parent.stat().st_mode
        except OSError:
            return ()
        group = group and bool(mode & (stat.S_IXGRP | stat.S_IXOTH))
        others = others and bool(mode & stat.S_IXOTH)
    return tuple(who for who, can in (("group", group), ("others", others)) if can)


def _who(classes: Iterable[str]) -> str:
    found = set(classes)
    if found == {"group", "others"}:
        return "its group and other users"
    return "its group" if found == {"group"} else "other users"


def _has_key(path: Path) -> bool:
    return any(find_secrets(line) for _, line in iter_lines(path))


def _files(artifact: Artifact) -> list[Path]:
    paths = [artifact.path]
    if is_sqlite(artifact.path):
        paths += [artifact.path.with_name(artifact.path.name + s) for s in _SQLITE_SIDECARS]
    return paths


class ReadableByOthers:
    id = "AP008"
    severity = Severity.MEDIUM  # high for credential stores and files holding a key
    title = "Agent data readable by other users"
    fix = "Make the file private to you: chmod 600 <file>."

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        if _is_windows():
            return []
        exposed: dict[tuple[str, Path], list[tuple[Path, Artifact, tuple[str, ...]]]]
        exposed = defaultdict(list)
        for artifact in artifacts:
            for path in _files(artifact):
                classes = readers(path)
                if classes:
                    exposed[(artifact.agent, path.parent)].append((path, artifact, classes))
        return [self._finding(agent, folder, files) for (agent, folder), files in exposed.items()]

    def _finding(
        self, agent: str, folder: Path, files: list[tuple[Path, Artifact, tuple[str, ...]]]
    ) -> Finding:
        high = any(
            artifact.kind is Kind.CREDENTIALS or _has_key(path) for path, artifact, _ in files
        )
        who = _who(c for _, _, classes in files for c in classes)
        first, artifact, _ = files[0]
        label = _LABEL.get(artifact.kind, "agent file")
        mode = f"{stat.S_IMODE(first.stat().st_mode):04o}"
        if len(files) == 1:
            location = str(first)
            detail = f"{label} is readable by {who} (mode {mode})"
            fix = f"chmod 600 {shlex.quote(str(first))}"
        else:
            location = str(folder)
            name = safe_snippet(first.name, 60)
            detail = f"{len(files)} {agent} files here are readable by {who}, e.g. {name} ({mode})"
            if len(files) <= _LISTED:
                fix = "chmod 600 " + " ".join(shlex.quote(str(path)) for path, _, _ in files)
            else:
                fix = f"find {shlex.quote(str(folder))} -maxdepth 1 -type f -exec chmod 600 {{}} +"
        return Finding(
            check_id=self.id,
            severity=Severity.HIGH if high else Severity.MEDIUM,
            title=self.title,
            agent=agent,
            location=location,
            line=None,
            detail=detail,
            fix=fix,
            count=len(files),
        )


CHECK = ReadableByOthers()
