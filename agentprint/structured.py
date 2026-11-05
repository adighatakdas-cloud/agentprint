"""Safe loading of agent config files (JSON, JSONC, TOML). Never raises.

A broken, half-written, binary or huge file loads as None: the structured checks (AP003-AP005)
then skip it, while the line-based secret checks still read it as text.
"""

from __future__ import annotations

import json
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from .files import iter_lines

MAX_BYTES = 32 * 1024 * 1024


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas, leaving string contents untouched.

    VS Code-family settings (Cursor, Windsurf) and opencode.jsonc allow both.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # trailing comma: drop it
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@lru_cache(maxsize=256)
def _load_cached(path: str, _mtime_ns: int, _size: int) -> Any:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        if path.endswith(".toml"):
            return tomllib.loads(text)
        try:
            return json.loads(text)
        except ValueError:
            return json.loads(strip_jsonc(text))
    except Exception:  # any parse failure (incl. RecursionError) means "no structure"
        return None


def load(path: Path) -> Any:
    """Parsed content of a JSON/JSONC/TOML file, or None if it can't be read or parsed.

    The result is cached per (path, mtime, size) and shared: callers must not mutate it.
    """
    try:
        st = path.stat()
        if not path.is_file() or st.st_size > MAX_BYTES:
            return None
    except OSError:
        return None
    return _load_cached(str(path), st.st_mtime_ns, st.st_size)


def find_line(path: Path, needle: str) -> int | None:
    """1-based line of the first occurrence of needle in a text file, else None."""
    if not needle:
        return None
    for number, line in iter_lines(path):
        if needle in line:
            return number
    return None
