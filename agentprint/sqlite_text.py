"""Read the text stored in an agent's SQLite database (Cursor state.vscdb, opencode.db).

Safety: the database is opened read-only through a `mode=ro` URI, and with `immutable=1` as
well when it has no -wal, -shm or -journal file next to it (an agent that is not running): a
read-only open of a WAL-mode database would otherwise create its -wal and -shm files. With those
files present (the agent may be writing) it is read through them, so recent rows are seen; that
open creates nothing either. Only real tables are read (never views), binary values are skipped,
and every value and the total amount of text are capped. A SQLite error never crashes or hangs a
scan: it ends the read, and the database is named in the scan's not_checked notes (unread.py),
so a store that could not be read is never taken for a clean one.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from . import unread

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


_SIDECARS = ("-wal", "-shm", "-journal")


def _uri(path: Path) -> str:
    """mode=ro, plus immutable=1 when no sidecar file exists (so none is created)."""
    real = path.resolve()
    uri = real.as_uri() + "?mode=ro"
    if not any(real.with_name(real.name + suffix).exists() for suffix in _SIDECARS):
        uri += "&immutable=1"
    return uri


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(_uri(path), uri=True, timeout=1.0)
    con.text_factory = bytes  # undecodable TEXT must not abort the read
    for pragma in ("PRAGMA query_only = ON", "PRAGMA trusted_schema = OFF"):
        with contextlib.suppress(sqlite3.Error):
            con.execute(pragma)
    return con


def iter_text_lines(path: Path) -> Iterator[str]:
    """Lines of text from every TEXT/BLOB cell of every table, within the size caps."""
    try:
        con = _connect(path)
    except (sqlite3.Error, OSError, ValueError) as exc:
        unread.note(path, unread.why(exc))
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
            except sqlite3.Error as exc:  # one table: read the others, but say so
                unread.note(path, unread.why(exc) + ", in part")
                continue
    except sqlite3.Error as exc:
        unread.note(path, unread.why(exc))
        return
    finally:
        con.close()
