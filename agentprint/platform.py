"""Which operating system agentprint runs on, in one place so tests can swap it.

Tests monkeypatch `is_windows` (never `os.name`, which breaks pathlib), and callers reach it as
`platform.is_windows()` at call time. Notes for features not available on Windows yet are shown
once per run, on stderr, so a silent no-op never looks like a clean result.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path, PurePath

NOT_CHECKED_WINDOWS = "agentprint: file permissions are not checked on Windows yet."
NO_HOOKS_WINDOWS = "Hooks are not installed on Windows yet; the MCP server and the note are."

_shown: set[str] = set()


def is_windows() -> bool:
    return os.name == "nt"


def note_once(message: str) -> bool:
    """Print `message` to stderr the first time only. True if it was printed now."""
    if message in _shown:
        return False
    _shown.add(message)
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(message + "\n")
    return True


def drive_safe_parts(path: PurePath) -> tuple[str, ...]:
    """A path's parts with the drive kept as a plain folder name, so a backup folder can mirror
    the path without it becoming absolute again: C:\\x\\y -> (C, x, y); a UNC path
    \\\\server\\share\\f -> (UNC, server, share, f); /x/y -> (x, y)."""
    drive = path.drive
    rest = tuple(p for p in path.parts[1:] if p) if path.anchor else path.parts
    if not drive:
        return rest
    if drive.startswith(("\\\\", "//")):
        names = [n for n in drive.replace("\\", "/").split("/") if n]
        return ("UNC", *(_clean(n) for n in names), *rest)
    return (_clean(drive.replace(":", "")), *rest)


def _clean(name: str) -> str:
    return name.replace(":", "").strip() or "_"


def roaming_appdata(home: PurePath) -> Path:
    """Windows' %APPDATA% (roaming profile), which can be redirected away from ~/AppData/Roaming.

    Only for the real home folder on Windows; any other home (a --home folder, a test) keeps its
    own AppData/Roaming, so a scan of another folder never reads this user's live one."""
    configured = os.environ.get("APPDATA")
    if is_windows() and configured and Path(home) == Path.home():
        return Path(configured)
    return Path(home) / "AppData" / "Roaming"
