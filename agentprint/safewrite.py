"""Safe file writes shared by `agentprint install` and `agentprint fix`.

Writes go through symlinks (dotfile setups keep their links), replace a file atomically with a
temp file that is private from its first byte, and keep the file's mode. Folders that hold
agentprint's own backups and journals are made private to the user.
"""

from __future__ import annotations

import contextlib
import os
import stat
import time
from pathlib import Path

REPLACE_TRIES = 5
REPLACE_FIRST_DELAY = 0.05  # 50, 100, 200, 400 ms between the five tries
WINDOWS_BUSY = (5, 32)  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION


class FileInUse(PermissionError):
    """Another program (an editor, an antivirus scan, the agent itself) holds the file."""

    reason = "file in use, left as is"

    def __init__(self) -> None:
        super().__init__(13, self.reason)

    def __str__(self) -> str:
        return self.reason


class FileReadOnly(FileInUse):
    """The file is read-only (someone locked it on purpose): waiting would not help."""

    reason = "read-only, left as is"


def _busy(exc: OSError) -> bool:
    """A Windows "file is open elsewhere" refusal (sharing violation, or access denied while the
    other program holds it). A plain POSIX permission error has no winerror and is not."""
    return isinstance(exc, PermissionError) and getattr(exc, "winerror", None) in WINDOWS_BUSY


def _read_only(target: Path) -> bool:
    try:
        return target.exists() and not os.access(target, os.W_OK)
    except OSError:
        return False


def _replace(tmp: Path, target: Path) -> None:
    """os.replace, tried again a few times when Windows says the target is in use."""
    delay = REPLACE_FIRST_DELAY
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            if not _busy(exc):
                raise
            if getattr(exc, "winerror", None) == 5 and _read_only(target):
                raise FileReadOnly() from exc
            if attempt == REPLACE_TRIES - 1:
                raise FileInUse() from exc
        time.sleep(delay)
        delay *= 2


def real(path: Path) -> Path:
    """Edit through symlinks (dotfile setups) instead of replacing the link."""
    return path.resolve() if path.is_symlink() else path


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file that is private (0600) from its first byte, then give it the
    original file's mode (or `mode`, or 0600 for a new file) and move it into place."""
    target = real(path)
    tmp = target.with_name(f".{target.name}.agentprint-tmp")
    try:
        tmp.unlink()  # a leftover from an interrupted run
    except FileNotFoundError:
        pass
    except PermissionError as exc:  # Windows: something still holds the leftover open
        raise FileInUse() from exc
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        final = stat.S_IMODE(target.stat().st_mode) if target.exists() else (mode or 0o600)
        os.chmod(tmp, final)
        _replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def private_dir(directory: Path) -> None:
    """Make a folder and every folder under it readable by the user only (0700)."""
    for d in [directory, *[p for p in directory.rglob("*") if p.is_dir()]]:
        d.chmod(stat.S_IRWXU)
