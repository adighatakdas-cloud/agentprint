"""A fake `uvx` for tests of agentprint's plugins: no download, no network.

`real_guard_uvx` answers `uvx --from agentprint==<version> agentprint <args>` (what every plugin
hook and MCP server runs) by running this checkout's agentprint with <args>. Each call's
arguments are written to `<bin_dir>/args`.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path


def fake_uvx(bin_dir: Path, body: str) -> Path:
    """`<bin_dir>/uvx`, a /bin/sh script that records its arguments, then runs `body`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    uvx = bin_dir / "uvx"
    uvx.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{bin_dir}/args"\n{body}\n')
    uvx.chmod(0o755)
    return uvx


def real_guard_uvx(bin_dir: Path) -> Path:
    """A uvx that runs this checkout's agentprint (guard, mcp, ...) for the plugins' commands."""
    python = shlex.quote(sys.executable)
    code = shlex.quote("from agentprint.cli import app; app()")
    body = f'[ "$1" = --from ] && shift 2\nshift\nexec {python} -c {code} "$@"'
    return fake_uvx(bin_dir, body)
