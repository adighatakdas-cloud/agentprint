"""Safe file writes shared by `agentprint install` and `agentprint fix`.

Writes go through symlinks (dotfile setups keep their links), replace a file atomically with a
temp file that is private from its first byte, and keep the file's mode. Folders that hold
agentprint's own backups and journals are made private to the user.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path


def real(path: Path) -> Path:
    """Edit through symlinks (dotfile setups) instead of replacing the link."""
    return path.resolve() if path.is_symlink() else path


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file that is private (0600) from its first byte, then give it the
    original file's mode (or `mode`, or 0600 for a new file) and move it into place."""
    target = real(path)
    tmp = target.with_name(f".{target.name}.agentprint-tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()  # a leftover from an interrupted run
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        final = stat.S_IMODE(target.stat().st_mode) if target.exists() else (mode or 0o600)
        os.chmod(tmp, final)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def private_dir(directory: Path) -> None:
    """Make a folder and every folder under it readable by the user only (0700)."""
    for d in [directory, *[p for p in directory.rglob("*") if p.is_dir()]]:
        d.chmod(stat.S_IRWXU)
