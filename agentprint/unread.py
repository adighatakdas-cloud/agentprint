"""Files a sweep wanted to read but could not (a locked database, a file it may not open).

Each is remembered for the current scan and reported through platform.scan_notes(), so a scan
that could not read an agent's store says so in every output (agent, JSON, human, MCP) instead
of ending CLEAR as if the store were clean. scanner.scan() forgets the previous scan's list.
"""

from __future__ import annotations

from pathlib import Path

from .sanitize import quote

_UNREAD: dict[str, str] = {}  # path -> why it could not be read


def note(path: Path, why: str) -> None:
    """Remember that `path` could not be read (the first reason given is kept)."""
    _UNREAD.setdefault(str(path), why)


def notes() -> tuple[str, ...]:
    return tuple(
        f"Could not read {quote(path)} ({why}): it was not checked for keys."
        for path, why in _UNREAD.items()
    )


def forget() -> None:
    _UNREAD.clear()


def why(exc: BaseException) -> str:
    """A short reason for a failed read, in plain words."""
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "locked"
    if isinstance(exc, PermissionError) or "permission" in text or "readonly" in text:
        return "permission denied"
    if "not a database" in text or "malformed" in text or "encrypted" in text:
        return "not a readable database"
    return "could not be opened"
