"""Defensive file access: skip binaries, huge files and unreadable paths instead of crashing."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from . import sqlite_text

MAX_BYTES = 512 * 1024 * 1024


def _is_binary(path: Path) -> bool:
    with path.open("rb") as fh:
        return b"\x00" in fh.read(8192)


def iter_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line_number, line). Yields nothing for binary, oversized or unreadable files.

    SQLite databases (Cursor, OpenCode chat stores) yield the text of their cells, read-only
    and size-capped, with line number 0 because database rows have no line.
    """
    if sqlite_text.is_sqlite(path):
        for line in sqlite_text.iter_text_lines(path):
            yield 0, line
        return
    try:
        if path.stat().st_size > MAX_BYTES or _is_binary(path):
            return
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            yield from enumerate(fh, start=1)
    except OSError:
        return


def walk_files(root: Path) -> Iterator[Path]:
    """Regular files at or under root. A symlinked root is followed (dotfile setups); symlinks
    inside directories are not. Missing or inaccessible paths yield nothing."""
    try:
        if root.is_file():
            yield root
            return
        if not root.is_dir():
            return
    except OSError:
        return
    for dirpath, _dirs, names in os.walk(root, followlinks=False):
        for name in sorted(names):
            path = Path(dirpath) / name
            try:
                if not path.is_symlink() and path.is_file():
                    yield path
            except OSError:
                continue
