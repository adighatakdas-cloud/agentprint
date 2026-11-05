"""Read the text stored in an agent's SQLite database (Cursor state.vscdb, opencode.db).

Safety: the database is opened read-only through a `mode=ro` URI (nothing is created, locked
for writing or changed), only real tables are read (never views), binary values are skipped,
and every value and the total amount of text are capped. Any SQLite error ends the read
quietly: a strange database must never crash or hang a scan.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

MAGIC = b"SQLite format 3\x00"
MAX_VALUE = 4 * 1024 * 1024  # read from one cell (characters for TEXT, bytes for BLOB)
MAX_TOTAL = 64 * 1024 * 1024  # characters read from one database


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _connect(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=1.0)
    con.text_factory = bytes  # undecodable TEXT must not abort the read
    for pragma in ("PRAGMA query_only = ON", "PRAGMA trusted_schema = OFF"):
        with contextlib.suppress(sqlite3.Error):
            con.execute(pragma)
    return con


def iter_text_lines(path: Path) -> Iterator[str]:
    """Lines of text from every TEXT/BLOB cell of every table, within the size caps."""
    try:
        con = _connect(path)
    except (sqlite3.Error, OSError, ValueError):
        return
    total = 0
    try:
        tables = [
            row[0].decode("utf-8", errors="replace")
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            try:
                columns = [
                    row[1].decode("utf-8", errors="replace")
                    for row in con.execute(f"PRAGMA table_info({_quote(table)})")
                ]
                if not columns:
                    continue
                # substr() caps each cell inside SQLite, so a huge cell is never fully loaded.
                capped = ", ".join(f"substr({_quote(c)}, 1, {int(MAX_VALUE)})" for c in columns)
                for row in con.execute(f"SELECT {capped} FROM {_quote(table)}"):
                    for value in row:
                        if not isinstance(value, bytes) or b"\x00" in value[:8192]:
                            continue
                        text = value.decode("utf-8", errors="replace")
                        total += len(text)
                        if total > MAX_TOTAL:
                            return
                        yield from text.splitlines()
            except sqlite3.Error:
                continue
    except sqlite3.Error:
        return
    finally:
        con.close()
