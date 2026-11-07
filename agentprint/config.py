"""Project config: `.agentprint.toml`, or `[tool.agentprint]` in pyproject.toml.

Read with the standard library's tomllib, nothing else. The first source found wins: the file
given with --config, then `.agentprint.toml` at the project root, then `[tool.agentprint]` in
the project's pyproject.toml; --no-config reads none of them. A config never stops a run: an
unknown key is one warning, a file that cannot be read is "could not read config <path>: ..."
and the defaults.

    fail_on = "medium"                     # like --fail-on; the command line wins
    strict = true                          # like guard --strict; it can only turn strict on
    baseline = ".agentprint-baseline.json" # accepted findings (the default name)
    [[ignore]]
    check = "AP003"                        # a check id, a path glob, or both
    path = ".devcontainer/**"
    reason = "throwaway dev container"

Ignore paths are globs over the path from the project root, with forward slashes: `*` and `?`
stay inside one folder, `**` crosses folders, a trailing `/` means everything under it. In a
package they match the file's name inside the package. They never match a file outside the
project (your home folder's agent files are yours, not the project's).
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .report import FailOn
from .sanitize import clean

CONFIG_FILE = ".agentprint.toml"
PYPROJECT = "pyproject.toml"
DEFAULT_BASELINE = ".agentprint-baseline.json"
MAX_CONFIG_BYTES = 1024 * 1024
KEYS = ("fail_on", "strict", "ignore", "baseline")
IGNORE_KEYS = ("check", "path", "reason")
NO_REASON = "no reason given"
CHECK_ID = re.compile(r"AP\d{3}")


@dataclass(frozen=True)
class Sources:
    """Where the command line says to look: --config, --no-config, --baseline."""

    config: Path | None = None
    off: bool = False
    baseline: Path | None = None


@dataclass(frozen=True)
class Ignore:
    check: str | None  # "AP003"; None = any check
    path: str | None  # glob from the project root; None = any file in the project
    reason: str
    pattern: re.Pattern[str] | None = field(default=None, compare=False, repr=False)

    def matches(self, check_id: str, path: str) -> bool:
        if self.check is not None and self.check != check_id:
            return False
        return self.pattern is None or self.pattern.fullmatch(path) is not None


@dataclass(frozen=True)
class Config:
    root: Path  # the project root: relative paths in the config are read from here
    source: Path | None = None  # the file read; None: the defaults
    fail_on: FailOn | None = None
    strict: bool = False
    ignore: tuple[Ignore, ...] = ()
    baseline: Path | None = None  # None: no baseline (--no-config)
    warnings: tuple[str, ...] = ()


def glob_pattern(glob: str) -> re.Pattern[str]:
    """A path glob as a regex over a POSIX path: * and ? stay in one folder, ** crosses them."""
    glob = glob.strip().replace("\\", "/").lstrip("/")
    if glob.endswith("/"):
        glob += "**"
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out), re.S)


def read_regular(path: Path, limit: int, *, links: bool = True) -> bytes:
    """A regular file's bytes, at most `limit` of them. Never blocks: a FIFO, a terminal or a
    device is refused before it is opened, and the file is opened non-blocking and checked
    again, so a planted /dev/stdin can't hold a hook until it times out (and lets a key
    through). `links` False also refuses a symbolic link. Raises FileNotFoundError when there
    is no file, and OSError or ValueError when it can't be used."""
    if not links and path.is_symlink():
        raise ValueError("it is a symbolic link, which agentprint does not follow here")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    if not links:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):  # swapped in after the first look
            raise ValueError("not a regular file")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > limit:
        mib = limit // (1024 * 1024)
        raise ValueError(f"larger than {mib} MiB" if mib else f"larger than {limit} bytes")
    return b"".join(chunks)


def _read_toml(data: bytes) -> dict:
    """The TOML text as a dict. Raises ValueError (tomllib's error is one)."""
    return tomllib.loads(data.decode("utf-8"))


def _why(exc: Exception) -> str:
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    return str(exc) or type(exc).__name__


def _find(root: Path, sources: Sources) -> tuple[Path | None, dict | None, list[str]]:
    """The config file and its table, first found wins. (None, None) when there is none."""
    given = sources.config
    if given is not None:
        candidates = [(given, given.name == PYPROJECT)]
    else:
        candidates = [(root / CONFIG_FILE, False), (root / PYPROJECT, True)]
    for path, in_pyproject in candidates:
        if given is None and not path.is_file():
            continue
        name = clean(str(path))
        try:
            raw = read_regular(path, MAX_CONFIG_BYTES)
        except (OSError, ValueError) as exc:
            if in_pyproject and given is None:
                continue  # not a config file of ours, just the project's metadata
            why = clean(_why(exc))
            return path, None, [f"could not read config {name}: {why}; using the defaults"]
        if in_pyproject and given is None and b"agentprint" not in raw:
            continue  # a pyproject.toml that never names agentprint is not ours to judge
        try:
            data = _read_toml(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            why = clean(_why(exc))
            return path, None, [f"could not read config {name}: {why}; using the defaults"]
        if not in_pyproject:
            return path, data, []
        tool = data.get("tool")
        table = tool.get("agentprint") if isinstance(tool, dict) else None
        if table is None and given is None:
            continue
        if not isinstance(table, dict):
            return path, None, [f"no [tool.agentprint] table in {name}; using the defaults"]
        return path, table, []
    return None, None, []


def load(root: Path, sources: Sources | None = None) -> Config:
    """The project's config. Never raises: problems become warnings and defaults."""
    sources = sources or Sources()
    if sources.off:
        return Config(root, baseline=sources.baseline)
    path, table, warnings = _find(root, sources)
    if table is None:
        baseline = sources.baseline or root / DEFAULT_BASELINE
        return Config(root, baseline=baseline, warnings=tuple(warnings))
    name = clean(str(path))
    unknown = [clean(str(key)) for key in table if key not in KEYS]

    fail_on = None
    if "fail_on" in table:
        value = table["fail_on"]
        if isinstance(value, str) and value.strip().lower() in FailOn.__members__:
            fail_on = FailOn(value.strip().lower())
        else:
            choices = ", ".join(FailOn)
            warnings.append(f"fail_on in {name} must be one of {choices}; using the default")

    strict = False
    if "strict" in table:
        if isinstance(table["strict"], bool):
            strict = table["strict"]
        else:
            warnings.append(f"strict in {name} must be true or false; strict mode stays off")

    baseline = root / DEFAULT_BASELINE
    if "baseline" in table:
        value = table["baseline"]
        named = Path(value.strip()) if isinstance(value, str) and value.strip() else None
        # Inside the project only: `agentprint baseline` writes it, and a cloned repository's
        # config must not point that write at a file elsewhere (~/.bashrc, ../other).
        if named is not None and not named.anchor and ".." not in named.parts:
            baseline = root / named
        else:
            warnings.append(
                f"baseline in {name} must be a file name inside the project; "
                f"using {DEFAULT_BASELINE}"
            )

    rules = _ignores(table.get("ignore", []), name, unknown, warnings)
    if unknown:
        warnings.insert(0, f"unknown keys in {name} (ignored): {', '.join(unknown)}")
    return Config(
        root,
        path,
        fail_on,
        strict,
        tuple(rules),
        sources.baseline or baseline,
        tuple(warnings),
    )


def _ignores(raw: object, name: str, unknown: list[str], warnings: list[str]) -> list[Ignore]:
    if not isinstance(raw, list):
        warnings.append(f"ignore in {name} must be a list of tables ([[ignore]]); none applied")
        return []
    from .checks import PACKAGE_CHECK_IDS, SCAN_CHECKS  # the checks import a lot: only now

    known_ids = {check.id for check in SCAN_CHECKS} | set(PACKAGE_CHECK_IDS)
    rules = []
    for n, entry in enumerate(raw):
        where = f"ignore entry {n + 1} in {name}"
        if not isinstance(entry, dict):
            warnings.append(f"{where} is not a table; skipped")
            continue
        unknown += [
            f"{clean(str(key))} (ignore entry {n + 1})" for key in entry if key not in IGNORE_KEYS
        ]
        check, path, reason = entry.get("check"), entry.get("path"), entry.get("reason", NO_REASON)
        if not all(v is None or isinstance(v, str) for v in (check, path, reason)):
            warnings.append(f"{where}: check, path and reason must be text; skipped")
            continue
        check = check.strip().upper() if check is not None else None
        if check is not None and (not CHECK_ID.fullmatch(check) or check not in known_ids):
            warnings.append(f"{where}: unknown check {clean(check)!r}; skipped")
            continue
        path = path.strip() if path is not None and path.strip() else None
        if check is None and path is None:
            warnings.append(
                f"{where} has neither check nor path (it would hide everything); skipped"
            )
            continue
        pattern = glob_pattern(path) if path is not None else None
        reason = clean(reason or NO_REASON, 200)
        rules.append(Ignore(check, path, reason, pattern))
    return rules
