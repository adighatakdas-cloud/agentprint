"""PowerShell text for the Windows hook launcher and the hook commands that start it.

Ruling (how a string goes into PowerShell code): as an ASCII-only PowerShell expression. Runs
of plain characters go in single quotes; every other character is written `[char]N`. So a
launcher path with a space, an apostrophe, a `%`, a quote-like character (PowerShell reads
U+2018, U+2019, U+201C and U+201D as quotes) or a non-ASCII letter (C:\\Users\\Zoë) reaches
PowerShell exactly as it is, through every layer around it: cmd.exe /C (where `%` expands even
inside double quotes), a command line PowerShell splits itself, or an outer PowerShell that
expands `$` and backticks inside double quotes. Windows PowerShell 5.1 reads a .ps1 file
without a byte-order mark in the ANSI code page, so the launcher file is ASCII too. Cost if
wrong: none in behaviour; the generated text is longer than hand-written PowerShell.
"""

from __future__ import annotations

import re
import string

# Never special to cmd.exe inside double quotes, to PowerShell inside single or double quotes,
# or to a Windows command line: letters, digits, space and \ / : . _ - + , = @ # ~ [ ] { } ( ).
PLAIN = frozenset(string.ascii_letters + string.digits + " \\/:._-+,=@#~[]{}()")
# Also plain inside a .ps1 file, which no shell reads first (a single quote never is).
IN_FILE = PLAIN | frozenset('"$`%!^;&|<>?*')


def _units(char: str) -> list[int]:
    """UTF-16 code units: PowerShell's [char] is one unit, so U+1F600 takes two."""
    data = char.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


def literal(text: str, plain: frozenset[str] = PLAIN) -> str:
    """A PowerShell expression whose value is exactly `text`, in printable ASCII: `'a b'`, or
    `('C:\\Users\\O' + [char]39 + 'Brien')` when a character needs [char]. Usable as a
    command argument (`Test-Path -LiteralPath <it>`, `& <it>`) and in an expression."""
    parts: list[str] = []
    run = ""
    for char in text:
        if char in plain:
            run += char
            continue
        if run:
            parts.append(f"'{run}'")
            run = ""
        parts.extend(f"[char]{unit}" for unit in _units(char))
    if run or not parts:
        parts.append(f"'{run}'")
    if len(parts) == 1 and parts[0].startswith("'"):
        return parts[0]
    return "(" + " + ".join(parts) + ")"


_WORD = re.compile(r"(?:--)?[A-Za-z][A-Za-z0-9_-]*")


def word(text: str) -> str:
    """A command argument: bare when it is a plain word (`--agent`, `claude-code`), else quoted.
    PowerShell reads a bare `--agent` as the text "--agent" (a single dash would make it a
    parameter name, so `-x` is quoted)."""
    return text if _WORD.fullmatch(text) else literal(text)
