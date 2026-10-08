"""Defensive file access: skip binaries, huge files and unreadable paths instead of crashing."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

MAX_BYTES = 512 * 1024 * 1024


def _is_binary(path: Path) -> bool:
    with path.open("rb") as fh:
        return b"\x00" in fh.read(8192)


def iter_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line_number, line). Yields nothing for binary, oversized or unreadable files."""
    try:
        if path.stat().st_size > MAX_BYTES or _is_binary(path):
            return
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            yield from enumerate(fh, start=1)
    except OSError:
        return


def walk_files(root: Path) -> Iterator[Path]:
    """Regular files at or under root. Symlinks are never followed; missing root yields nothing."""
    if root.is_symlink():
        return
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    for dirpath, _dirs, names in os.walk(root, followlinks=False):
        for name in sorted(names):
            path = Path(dirpath) / name
            if not path.is_symlink() and path.is_file():
                yield path
