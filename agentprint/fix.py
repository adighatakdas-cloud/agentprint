"""`agentprint fix`: turn prints (findings) into a plan of safe changes, then apply it.

`plan()` reads the files the findings point at and decides, for each print, what agentprint
changes itself (a Change) and what only the human can do (a Manual item: rotating a key,
anything that would change an agent's behaviour). `apply()` makes the changes and records each
one in a journal, ~/.agentprint/fix/<UTC stamp>.json (0600, folder 0700), that never holds a key.

Masking (AP001 keys in history files, AP002 keys inside permission-list entries) overwrites each
key's bytes in place with its first four characters and `*` to the same length. It is a
positioned write at the key's byte offset, made only after re-reading those bytes and checking
they still hold what was planned. The file is never renamed or truncated, so text a running
agent appends meanwhile is kept, and no copy of a key is ever made: no backup, and only masked
stubs (`redact()` form) in the plan, the journal and the output.

New kinds of change plug in through PLANNERS (check id -> planner) and APPLIERS (kind ->
applier); a print with no planner is left to the human with its existing advice.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import sqlite_text, structured
from .advice import Moment, advise, split_location
from .files import MAX_BYTES
from .models import Finding
from .safewrite import atomic_write, private_dir
from .sanitize import quote
from .secrets import find_secrets, redact

JOURNAL_DIR = Path(".agentprint") / "fix"  # under the home folder
STAMP = "%Y%m%dT%H%M%SZ"  # UTC; also names the run's backup folder
CHANGED = "the file changed while fixing; run agentprint fix again"
GONE = "the file is no longer there; run agentprint fix again"

# Key names whose entries are pasted commands or tool patterns, not config values: masking a key
# inside one changes no behaviour. An entry counts when it is a string in a list under one of
# these names, or a key of a map under one (VS Code and OpenCode key their allowlists by
# command). A name is matched as the last key, or as the last two keys joined by a dot.
ALLOWLIST_KEYS: frozenset[str] = frozenset(
    {
        # Claude Code permissions.{allow,deny,ask}; Cursor and Antigravity permissions.allow and
        # .deny; Hermes approvals.deny.
        "allow",
        "deny",
        "ask",
        "allowedTools",  # Claude Code ~/.claude.json projects.<path>.allowedTools
        "autoApprove",  # MCP tool auto-approval lists
        "trustedCommands",
        "tools.allowed",  # Gemini CLI; tools.core and tools.exclude take the same patterns
        "tools.core",
        "tools.exclude",
        "windsurf.cascadeCommandsAllowList",  # Windsurf (flat VS Code-style key names)
        "windsurf.cascadeCommandsDenyList",
        "command_allowlist",  # Hermes Agent
        "chat.tools.terminal.autoApprove",  # Copilot in VS Code: {command prefix: true}
        "permission.bash",  # OpenCode: {pattern: "allow" | "ask" | "deny"}
    }
)

# How many key occurrences in one settings file are tested one by one (each test parses the
# whole file); the rest are left to the human.
MAX_SETTINGS_TRIES = 64


# ---- the plan --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One edit agentprint makes itself. `shown` is a masked stub or plain words, never a key."""

    kind: str  # "mask" | "env" | "ignore" | "lock"
    path: Path
    line: int | None
    check_id: str
    shown: str
    # mask: the byte spans [start, end) to overwrite, and a digest of what each span held when
    # planned, checked again right before writing. The key itself is never kept.
    offsets: tuple[tuple[int, int], ...] = ()
    digests: tuple[bytes, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Manual:
    """One "needs you" item: something only the human can do, already sanitised for display."""

    check_id: str
    text: str


@dataclass
class Plan:
    changes: list[Change]
    manual: list[Manual]
    findings: list[Finding]


@dataclass
class Outcome:
    applied: list[Change]
    skipped: list[tuple[Change, str]]  # (change, plain-words reason)
    journal: Path | None  # None when nothing was changed


Planner = Callable[[list[Finding], bool], tuple[list[Change], list[Manual]]]


def mask_text(value: str) -> str:
    """The key's first 4 characters, then `*` to its full length: same length, nothing usable."""
    return value[:4] + "*" * (len(value) - 4)


def _mask_bytes(data: bytes) -> bytes:
    return data[:4] + b"*" * (len(data) - 4)


# A key that lives only in this process: a plan's digests can't be used to test guesses of a key,
# even if a plan were ever printed or saved.
_DIGEST_KEY = os.urandom(32)


def _digest(data: bytes) -> bytes:
    return hashlib.blake2b(data, key=_DIGEST_KEY, digest_size=32).digest()


def plan(findings: list[Finding], *, pack: bool = False) -> Plan:
    """What `fix` would do about these findings. Reads files, never changes them.

    `pack`: the findings come from `agentprint pack` (advice is worded for a publish)."""
    changes: list[Change] = []
    manual: list[Manual] = []
    groups: dict[str, list[Finding]] = {}
    for finding in findings:
        groups.setdefault(finding.check_id, []).append(finding)
    for check_id, group in groups.items():
        planner = PLANNERS.get(check_id, _leave_to_human)
        more_changes, more_manual = planner(group, pack)
        changes += more_changes
        manual += more_manual
    return Plan(changes, list(dict.fromkeys(manual)), list(findings))


def _moment(pack: bool) -> Moment:
    return Moment.publish if pack else Moment.scan


def _advice(finding: Finding, pack: bool) -> Manual:
    """The existing advice for a print, as one "needs you" item."""
    advice = advise(finding, _moment(pack))
    return Manual(finding.check_id, " ".join([*advice.fix_steps, *advice.ask_human]))


def _leave_to_human(findings: list[Finding], pack: bool) -> tuple[list[Change], list[Manual]]:
    """Prints agentprint never changes itself (AP003, AP004, ...): where, what, and the advice."""
    manual = []
    for finding in findings:
        line = f" line {finding.line}" if finding.line else ""
        where = f"{finding.title} in {quote(finding.location)}{line} ({quote(finding.detail)})"
        manual.append(Manual(finding.check_id, f"{where}: {_advice(finding, pack).text}"))
    return [], manual


# ---- masking: AP001 (history) and AP002 (permission-list entries in settings) ---------------


@dataclass(eq=False)
class _Span:
    """Bytes [start, end) of a file holding one key, or several overlapping keys."""

    start: int
    end: int
    keys: list[str]


def _read(path: Path) -> bytes | None:
    try:
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _keys_in(data: bytes) -> dict[str, str]:
    """Every key in a file -> its rule name, in order of first appearance. Lines are read the
    way the scan reads them: split on newlines, invalid UTF-8 replaced."""
    found: dict[str, str] = {}
    for raw in data.split(b"\n"):
        for match in find_secrets(raw.decode("utf-8", errors="replace")):
            found.setdefault(match.value, match.name)
    return found


def _spans(data: bytes, keys: Iterable[str]) -> list[_Span]:
    """Every occurrence of every key in the raw bytes, overlapping ones merged, in file order."""
    found: list[tuple[int, int, str]] = []
    for key in keys:
        needle = key.encode("ascii")
        start = data.find(needle)
        while start != -1:
            found.append((start, start + len(needle), key))
            start = data.find(needle, start + 1)
    merged: list[_Span] = []
    for start, end, key in sorted(found):
        if merged and start < merged[-1].end:
            merged[-1].end = max(merged[-1].end, end)
            if key not in merged[-1].keys:
                merged[-1].keys.append(key)
        else:
            merged.append(_Span(start, end, [key]))
    return merged


def _masked(data: bytes, spans: Iterable[_Span], shift: int = 0) -> bytes:
    out = bytearray(data)
    for span in spans:
        start, end = span.start - shift, span.end - shift
        out[start:end] = _mask_bytes(bytes(out[start:end]))
    return bytes(out)


def _breaks_json(before: bytes, after: bytes) -> bool:
    """True if `before` is JSON and `after` no longer is."""
    try:
        json.loads(before)
    except (ValueError, RecursionError):
        return False  # not JSON: nothing to keep
    try:
        json.loads(after)
    except (ValueError, RecursionError):
        return True
    return False


def _history_spans(path: Path, data: bytes, spans: list[_Span]) -> list[_Span]:
    """Every key in a history file, except where masking would break a JSON line (or a whole
    JSON file): a key read from a JSON escape such as \\u0041... would lose half the escape."""
    lines: dict[tuple[int, int], list[_Span]] = {}
    for span in spans:
        start = data.rfind(b"\n", 0, span.start) + 1
        end = data.find(b"\n", span.end)
        lines.setdefault((start, len(data) if end == -1 else end), []).append(span)
    accepted: list[_Span] = []
    for (start, end), group in lines.items():
        line = data[start:end]
        if not _breaks_json(line, _masked(line, group, shift=start)):
            accepted += group
    whole_file = accepted and len(data) <= structured.MAX_BYTES
    if whole_file and _breaks_json(data, _masked(data, accepted)):
        return []
    return accepted


def _is_allowlist(path: tuple[Any, ...]) -> bool:
    if not path or not isinstance(path[-1], str):
        return False
    if path[-1] in ALLOWLIST_KEYS:
        return True
    return (
        len(path) >= 2 and isinstance(path[-2], str) and f"{path[-2]}.{path[-1]}" in ALLOWLIST_KEYS
    )


def _edited_entries(before: Any, after: Any, path: tuple[Any, ...]) -> list[tuple[Any, ...]] | None:
    """Where two parses of a file differ, as the paths of the lists (string entries changed) and
    maps (keys changed) that hold the differences. None if anything else differs: a config
    value, a type, the shape."""
    if type(before) is not type(after):
        return None
    if isinstance(before, dict):
        if len(before) != len(after):
            return None
        found: list[tuple[Any, ...]] = []
        for (k1, v1), (k2, v2) in zip(before.items(), after.items(), strict=True):
            if k1 != k2:
                if not (isinstance(k1, str) and isinstance(k2, str)):
                    return None
                found.append(path)
            inner = _edited_entries(v1, v2, (*path, k1))
            if inner is None:
                return None
            found += inner
        return found
    if isinstance(before, list):
        if len(before) != len(after):
            return None
        found = []
        for i, (v1, v2) in enumerate(zip(before, after, strict=True)):
            if isinstance(v1, str) and isinstance(v2, str):
                if v1 != v2:
                    found.append(path)
                continue
            inner = _edited_entries(v1, v2, (*path, i))
            if inner is None:
                return None
            found += inner
        return found
    return [] if before == after else None


def _only_allowlists_change(before: Any, after: Any) -> bool:
    edited = _edited_entries(before, after, ())
    return bool(edited) and all(_is_allowlist(p) for p in edited)


def _allowlist_spans(path: Path, data: bytes, spans: list[_Span]) -> list[_Span]:
    """Keys that sit inside a permission-list entry. The file is parsed the way its agent module
    parses it; each key occurrence is masked on its own and kept only if nothing but entries of
    an ALLOWLIST_KEYS list (or keys of such a map) changes. Config values are never touched."""
    if len(data) > structured.MAX_BYTES:
        return []
    name = str(path)
    before = structured.parse(data, name)
    if before is None:
        return []
    accepted = [
        span
        for span in spans[:MAX_SETTINGS_TRIES]
        if _only_allowlists_change(before, structured.parse(_masked(data, [span]), name))
    ]
    if accepted and not _only_allowlists_change(
        before, structured.parse(_masked(data, accepted), name)
    ):
        return []
    return accepted


def _mask_changes(
    path: Path, check_id: str, data: bytes, keys: Iterable[str], accepted: list[_Span]
) -> list[Change]:
    """One Change per key: every accepted span that starts with it (merged spans go to the
    first key in them)."""
    changes = []
    for key in keys:
        mine = [span for span in accepted if span.keys[0] == key]
        if mine:
            changes.append(
                Change(
                    kind="mask",
                    path=path,
                    line=data.count(b"\n", 0, mine[0].start) + 1,
                    check_id=check_id,
                    shown=redact(key),
                    offsets=tuple((s.start, s.end) for s in mine),
                    digests=tuple(_digest(data[s.start : s.end]) for s in mine),
                )
            )
    return changes


def _chat_database(finding: Finding) -> Manual:
    rotate = advise(finding, Moment.scan).ask_human
    text = (
        f"Delete the chat that holds this key in {finding.agent}: it is stored in the chat "
        f"database {quote(finding.location)}, which agentprint does not edit."
    )
    return Manual(finding.check_id, " ".join([text, *rotate]))


def _plan_masks(findings: list[Finding], pack: bool) -> tuple[list[Change], list[Manual]]:
    """AP001 and AP002: mask every key that can be masked safely. A masked key is still
    exposed, so each print keeps a "needs you" item: rotate, plus the existing advice for a key
    that is still in its file."""
    changes: list[Change] = []
    manual: list[Manual] = []
    by_file: dict[str, list[Finding]] = {}
    for finding in findings:
        by_file.setdefault(finding.location, []).append(finding)
    for location, group in by_file.items():
        path = Path(location)
        if split_location(location)[0] is not None:  # inside a package: fix the source
            manual += [_advice(f, pack) for f in group]
            continue
        if any(not f.line for f in group) or sqlite_text.is_sqlite(path):  # database rows
            manual += [
                _chat_database(f) if f.check_id == "AP001" else _advice(f, pack) for f in group
            ]
            continue
        data = _read(path)
        if data is None:
            manual += [_advice(f, pack) for f in group]
            continue
        keys = _keys_in(data)
        spans = _spans(data, [key for key in keys if key.isascii()])
        choose = _history_spans if group[0].check_id == "AP001" else _allowlist_spans
        accepted = choose(path, data, spans)
        changes += _mask_changes(path, group[0].check_id, data, keys, accepted)
        done = set(accepted)
        masked = {
            key
            for key in keys
            if (mine := [s for s in spans if key in s.keys]) and all(s in done for s in mine)
        }
        for finding in group:
            # Several keys can share a stub (same rule, first 4 characters and length).
            same = [key for key, rule in keys.items() if f"{rule}: {redact(key)}" == finding.detail]
            if same and all(key in masked for key in same):
                ask = advise(finding, _moment(pack)).ask_human
                manual += [Manual(finding.check_id, text) for text in ask]
            else:
                manual.append(_advice(finding, pack))
    return changes, manual


PLANNERS: dict[str, Planner] = {"AP001": _plan_masks, "AP002": _plan_masks}


# ---- applying --------------------------------------------------------------------------------


class Journal:
    """~/.agentprint/fix/<UTC stamp>.json: every change one run made (0600, folder 0700).

    Each entry: kind, path, line, check id, masked stub (`shown`) and `undo` (what --undo needs;
    null when the change can't be undone, such as a masked key). Never holds a key. The file
    is created at the first recorded change; a stamp already taken moves on by a second."""

    def __init__(self, home: Path, now: datetime | None = None) -> None:
        self.folder = home / JOURNAL_DIR
        self.started = now or datetime.now(UTC)
        self.path: Path | None = None
        self.entries: list[dict[str, Any]] = []

    @property
    def stamp(self) -> str:
        return self.started.strftime(STAMP)

    def open(self) -> Path:
        """Create the private folder and claim this run's file (idempotent)."""
        if self.path is None:
            self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_dir(self.folder)
            while (self.folder / f"{self.stamp}.json").exists():
                self.started += timedelta(seconds=1)
            self.path = self.folder / f"{self.stamp}.json"
            self._save()
        return self.path

    def record(self, change: Change, undo: dict[str, Any] | None = None) -> None:
        self.open()
        self.entries.append(
            {
                "kind": change.kind,
                "path": str(change.path),
                "line": change.line,
                "check_id": change.check_id,
                "shown": change.shown,
                "undo": undo,
            }
        )
        self._save()

    def _save(self) -> None:
        assert self.path is not None
        data = {"version": 1, "stamp": self.stamp, "changes": self.entries}
        atomic_write(self.path, json.dumps(data, indent=2) + "\n", 0o600)


@dataclass
class Result:
    """What an applier did with one change: reason None = applied; `undo` goes to the journal."""

    change: Change
    reason: str | None = None
    undo: dict[str, Any] | None = None


Applier = Callable[[list[Change], Journal], list[Result]]


def _cannot_write(exc: OSError) -> str:
    return f"agentprint could not write the file ({exc.strerror or type(exc).__name__}); left as is"


def _read_at(fd: int, offset: int, size: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


def _write_at(fd: int, offset: int, data: bytes) -> None:
    done = 0
    while done < len(data):
        if hasattr(os, "pwrite"):
            n = os.pwrite(fd, data[done:], offset + done)
        else:
            os.lseek(fd, offset + done, os.SEEK_SET)
            n = os.write(fd, data[done:])
        if n <= 0:
            raise OSError("short write")
        done += n


def _holds(fd: int, change: Change) -> bool:
    """True if every span still holds exactly the bytes that were planned."""
    return all(
        _digest(_read_at(fd, start, end - start)) == digest
        for (start, end), digest in zip(change.offsets, change.digests, strict=True)
    )


def _mask_one(fd: int, change: Change) -> str | None:
    for (start, end), digest in zip(change.offsets, change.digests, strict=True):
        current = _read_at(fd, start, end - start)  # read again right before writing
        if _digest(current) != digest:
            return CHANGED
        _write_at(fd, start, _mask_bytes(current))
    return None


def _apply_masks(changes: list[Change], journal: Journal) -> list[Result]:
    """Mask every planned key in one file in place: no rename, no truncation, no backup. If any
    planned span no longer holds its key (the file was rewritten, not just appended to), the
    whole file is skipped; a fresh `agentprint fix` plans it again."""
    path = changes[0].path
    try:
        # O_NONBLOCK: a file swapped for a FIFO since the scan must not hang the run.
        # O_BINARY (Windows only): no newline translation.
        flags = os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return [Result(c, GONE) for c in changes]
    except OSError as exc:
        return [Result(c, _cannot_write(exc)) for c in changes]
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return [Result(c, CHANGED) for c in changes]
        if not all(_holds(fd, c) for c in changes):
            return [Result(c, CHANGED) for c in changes]
        results = []
        for change in changes:
            try:
                results.append(Result(change, _mask_one(fd, change)))
            except OSError as exc:
                results.append(Result(change, _cannot_write(exc)))
        with contextlib.suppress(OSError):
            os.fsync(fd)
        return results
    except OSError as exc:
        return [Result(c, _cannot_write(exc)) for c in changes]
    finally:
        os.close(fd)


APPLIERS: dict[str, Applier] = {"mask": _apply_masks}


def apply(p: Plan, home: Path) -> Outcome:
    """Make the plan's changes, file by file, journaling each one under `home`. A change whose
    file changed or vanished since the plan is skipped with a reason; the others go ahead."""
    journal = Journal(home)
    applied: list[Change] = []
    skipped: list[tuple[Change, str]] = []
    groups: dict[tuple[str, Path], list[Change]] = {}
    for change in p.changes:
        groups.setdefault((change.kind, change.path), []).append(change)
    stop: str | None = None  # set when the journal can't be written: nothing more is changed
    try:
        for (kind, _path), changes in groups.items():
            applier = APPLIERS.get(kind)
            if stop or applier is None:
                reason = stop or f"agentprint can't make a {kind!r} change"
                skipped += [(c, reason) for c in changes]
                continue
            for result in applier(changes, journal):
                if result.reason is not None:
                    skipped.append((result.change, result.reason))
                    continue
                applied.append(result.change)
                try:
                    journal.record(result.change, result.undo)
                except OSError as exc:
                    why = exc.strerror or type(exc).__name__
                    stop = f"not changed: agentprint could not write its fix journal ({why})"
    finally:
        if applied:
            structured.forget()  # masked files keep their size: don't serve the old parse
    return Outcome(applied, skipped, journal.path)
