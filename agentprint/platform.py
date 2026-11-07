"""Which operating system agentprint runs on, in one place so tests can swap it.

Tests monkeypatch `is_windows` (never `os.name`, which breaks pathlib), and callers reach it as
`platform.is_windows()` at call time. What cannot be checked on Windows yet travels with the result
(scan_notes), so a silent no-op never reads as a clean result.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path, PurePath
from typing import Any

from . import unread

NOT_CHECKED_WINDOWS = "File permissions (AP008) are not checked on Windows yet."
NO_HOOKS_WINDOWS = "Hooks are not installed on Windows yet; the MCP server and the note are."


def is_windows() -> bool:
    return os.name == "nt"


def subprocess_kwargs() -> dict[str, Any]:
    """Extra `subprocess.run` arguments: on Windows, no console window flashes up for each git or
    npm call a background hook makes. Nothing on POSIX."""
    if not is_windows():
        return {}
    return {"creationflags": 0x08000000}  # subprocess.CREATE_NO_WINDOW


def use_utf8_streams() -> None:
    """Read stdin and write stdout/stderr as UTF-8 whatever the console code page is (cp1252 on
    most Windows machines), so non-ASCII JSON survives a hook round trip. Streams that cannot be
    reconfigured (test captures, closed streams) are left as they are."""
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, "strict"), (sys.stderr, "replace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors=errors)


def scan_notes() -> tuple[str, ...]:
    """What the last sweep could not check: on Windows, file permissions; anywhere, each file it
    could not read (a locked database: unread.py). Empty when everything was checked. Shown in
    every output format."""
    return ((NOT_CHECKED_WINDOWS,) if is_windows() else ()) + unread.notes()


def drive_safe_parts(path: PurePath) -> tuple[str, ...]:
    """A path's parts with the drive kept as a plain folder name, so a backup folder can mirror
    the path without it becoming absolute again: C:\\x\\y -> (C, x, y); a UNC path
    \\\\server\\share\\f -> (UNC, server, share, f); /x/y -> (x, y). The extended prefixes
    \\\\?\\ and \\\\.\\ are dropped first (\\\\?\\C:\\x -> (C, x); \\\\?\\UNC\\server\\share\\x
    -> (UNC, server, share, x))."""
    drive = path.drive
    rest = tuple(p for p in path.parts[1:] if p) if path.anchor else path.parts
    if not drive:
        return rest
    names = [n for n in drive.replace("\\", "/").split("/") if n]
    if drive[:2] in ("\\\\", "//"):
        device = bool(names) and names[0] == "."  # \\.\COM1; \\?\ is the long-path prefix
        if names and names[0] in ("?", "."):
            names = names[1:]
        if device:
            return ("DEVICE", *(_clean(n) for n in names), *rest)
        if names and names[0].upper() == "UNC":
            return ("UNC", *(_clean(n) for n in names[1:]), *rest)
        if names and len(names[0]) == 2 and names[0][1] == ":":
            return (_clean(names[0]), *rest)
        return ("UNC", *(_clean(n) for n in names), *rest)
    return (_clean(drive), *rest)


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
