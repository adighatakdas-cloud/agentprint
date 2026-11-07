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
stubs (`redact()` form) in the plan, the journal and the output. A key hidden on purpose (split
into joined strings, folded over lines, cut by invisible characters, base64) has no run of bytes
to mask: it is always left to the human.

New kinds of change plug in through PLANNERS (check id -> planner) and APPLIERS (kind ->
applier); a print with no planner is left to the human with its existing advice.
"""

from __future__ import annotations

import contextlib
import copy
import fnmatch
import hashlib
import json
import os
import re
import shlex
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import platform, sqlite_text, structured
from .advice import NO_LEAK, Advice, Moment, advise, agent_path, split_location
from .agents import ALL_AGENTS, by_name
from .checks.ap005_mcp_inline_secret import _MARKER as MCP_MARKER
from .checks.ap007_local_not_ignored import git_state
from .checks.base import safe_snippet
from .files import MAX_BYTES
from .models import Finding, Severity
from .safewrite import atomic_write, private_dir, real
from .sanitize import clean, quote
from .secrets import find_secrets, hidden_how, redact

JOURNAL_DIR = Path(".agentprint") / "fix"  # under the home folder
STAMP = "%Y%m%dT%H%M%SZ"  # UTC; also names the run's backup folder
CHANGED = "the file changed while fixing; run agentprint fix again"
GONE = "the file is no longer there; run agentprint fix again"

Version = tuple[int, int]  # a file's (mtime_ns, size)

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
    # env: the JSON path of the value as a JSON list (["mcpServers", "github", "env", "TOKEN"]),
    # the variable name, and (mtime_ns, size) of the file when planned. offsets/digests hold the
    # value's string token, quotes included.
    json_path: str = ""
    var: str = ""
    version: Version | None = None
    # ignore: the line to add, and the ignore file to add it to (also `path`, so every line for
    # one file is applied in one go).
    line_text: str = ""
    target: Path | None = None
    # lock: the file's mode when planned.
    old_mode: int | None = None
    # ignore: when the file does not exist yet, start it as a copy of this file (.npmignore from
    # .gitignore: npm stops reading .gitignore once .npmignore exists).
    seed: Path | None = None


@dataclass(frozen=True)
class Manual:
    """One "needs you" item: something only the human can do, already sanitised for display.

    `text` is worded for the AI agent that ran fix (the agent and JSON views); `person` is the
    same item for a person reading the plan (the human view), or "" when `text` reads well to
    both. Two items with the same check id and text are one item, whatever their `person`.

    `note`: nothing is wrong and nothing needs doing (AP009's low prints: an open-source
    package's maps, or a package for a private registry); shown as a note, never counted as
    something for the human to do."""

    check_id: str
    text: str
    person: str = field(default="", compare=False)
    note: bool = field(default=False, compare=False)


@dataclass
class Plan:
    changes: list[Change]
    manual: list[Manual]
    findings: list[Finding]

    @property
    def todo(self) -> list[Manual]:
        """The "needs you" items: what only the human can do."""
        return [m for m in self.manual if not m.note]

    @property
    def notes(self) -> list[Manual]:
        """Items to know about that need nothing done."""
        return [m for m in self.manual if m.note]


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


def plan(findings: list[Finding], *, pack: bool = False, root: Path | None = None) -> Plan:
    """What `fix` would do about these findings. Reads files, never changes them.

    `pack`: the findings come from `agentprint pack` (advice is worded for a publish).
    `root`: with `pack`, the project folder that was checked (not a built archive)."""
    changes: list[Change] = []
    manual: list[Manual] = []
    groups: dict[str, list[Finding]] = {}
    for finding in findings:
        groups.setdefault(finding.check_id, []).append(finding)
    for check_id, group in groups.items():
        if pack and check_id == "AP006":
            more_changes, more_manual = _plan_package(group, root)
        elif pack and check_id == "AP002":
            more_changes, more_manual = _plan_packaged_secrets(group, root)
        else:
            planner = PLANNERS.get(check_id, _leave_to_human)
            more_changes, more_manual = planner(group, pack)
        changes += more_changes
        manual += more_manual
    return Plan(changes, list(dict.fromkeys(manual)), list(findings))


def _moment(pack: bool) -> Moment:
    return Moment.publish if pack else Moment.scan


def _joined(advice: Advice) -> str:
    return " ".join([*advice.fix_steps, *advice.ask_human])


def _advice(finding: Finding, pack: bool) -> Manual:
    """The existing advice for a print, as one "needs you" item (for the agent, and the same
    steps worded for a person)."""
    moment = _moment(pack)
    return Manual(
        finding.check_id,
        _joined(advise(finding, moment)),
        _joined(advise(finding, moment, person=True)),
    )


def _ask_human(finding: Finding, pack: bool) -> list[Manual]:
    """The print's ask_human items (rotating its key), each one "needs you" item."""
    moment = _moment(pack)
    agent = advise(finding, moment).ask_human
    person = advise(finding, moment, person=True).ask_human
    return [Manual(finding.check_id, a, p) for a, p in zip(agent, person, strict=True)]


def _leave_to_human(findings: list[Finding], pack: bool) -> tuple[list[Change], list[Manual]]:
    """Prints agentprint never changes itself (AP003, AP004, ...): where, what, and the advice.
    The agent reads the scanned detail as a quoted value; a person reads it as a sentence, the
    way `agentprint scan` shows it."""
    manual = []
    for finding in findings:
        line = f" line {finding.line}" if finding.line else ""
        where = f"{finding.title} in {quote(finding.location)}{line}"
        advice = _advice(finding, pack)
        detail = clean(finding.detail)
        end = "" if detail.endswith((".", "!", "?")) else "."
        manual.append(
            Manual(
                finding.check_id,
                f"{where} ({quote(finding.detail)}): {advice.text}",
                f"{where}: {detail}{end} {advice.person}",
            )
        )
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


def _in_prompt_history(path: tuple[Any, ...]) -> bool:
    """Claude Code keeps pasted prompts in ~/.claude.json under projects.<path>.history[*]
    (display, pastedContents): text the user typed, not configuration."""
    return len(path) >= 3 and path[0] == "projects" and path[2] == "history"


def _is_allowlist(path: tuple[Any, ...]) -> bool:
    if _in_prompt_history(path):
        return True
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
    if before == after:
        return []
    # A changed string inside prompt history is an edited entry; elsewhere it is a config value.
    if isinstance(before, str) and _in_prompt_history(path):
        return [path]
    return None


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
    rotate = _ask_human(finding, False)
    text = (
        f"Delete the chat that holds this key in {finding.agent}: it is stored in the chat "
        f"database {quote(finding.location)}, which agentprint does not edit."
    )
    return Manual(
        finding.check_id,
        " ".join([text, *(m.text for m in rotate)]),
        " ".join([text, *(m.person for m in rotate)]),
    )


def _pure_history(finding: Finding, path: Path) -> bool:
    """True if the agent module says this history file is only a record (a transcript, chat
    log, paste cache or prompt history) that the agent never reads back."""
    agent = by_name(finding.agent)
    check = getattr(agent, "pure_history", None)
    try:
        return bool(check is not None and check(path))
    except Exception:  # an agent module bug must never let fix mask a file it can't place
        return False


def _read_back(finding: Finding, pack: bool) -> Manual:
    """AP001 in a file the agent reads back as instructions or config (a skill, rule, memory or
    scheduled job). The key is in use there, so masking it would break what uses it before the
    human has a new one: it stays with the human."""
    line = f" line {finding.line}" if finding.line else ""
    first = (
        f"Move the key out of {quote(finding.location)}{line}: {finding.agent} reads that file "
        "back as instructions or config (a skill, rule, memory or scheduled job). agentprint "
        "leaves the key there, since masking it would break what uses it. Load it from an "
        "environment variable or the agent's own key store instead."
    )
    rotate = _ask_human(finding, pack)
    return Manual(
        finding.check_id,
        " ".join([f"{first} {NO_LEAK}", *(m.text for m in rotate)]),
        " ".join([first, *(m.person for m in rotate)]),
    )


def _plan_masks(findings: list[Finding], pack: bool) -> tuple[list[Change], list[Manual]]:
    """AP001 and AP002: mask every key that can be masked safely. A masked key is still
    exposed, so each print keeps a "needs you" item: rotate, plus the existing advice for a key
    that is still in its file.

    AP001 masks only pure history, as its agent module names it (transcripts, chat logs, paste
    caches, prompt history). A key in a file the agent reads back (skills, rules, memories,
    scheduled jobs) is in use: that print is the human's. So is a hidden key."""
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
        # A key hidden on purpose (split, folded, invisible characters, base64) has no run of
        # bytes to mask: removing it is the human's, with its advice (find every piece, rotate).
        hidden = [f for f in group if hidden_how(f.detail) is not None]
        manual += [
            _chat_database(f) if f.check_id == "AP001" and not f.line else _advice(f, pack)
            for f in hidden
        ]
        group = [f for f in group if hidden_how(f.detail) is None]
        if not group:
            continue
        if any(not f.line for f in group) or sqlite_text.is_sqlite(path):  # database rows
            manual += [
                _chat_database(f) if f.check_id == "AP001" else _advice(f, pack) for f in group
            ]
            continue
        if group[0].check_id == "AP001" and not _pure_history(group[0], path):
            manual += [_read_back(f, pack) for f in group]
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
                manual += _ask_human(finding, pack)
            else:
                manual.append(_advice(finding, pack))
    return changes, manual


PLANNERS: dict[str, Planner] = {"AP001": _plan_masks, "AP002": _plan_masks}


# ---- env references: AP005 (Claude Code MCP entries) -----------------------------------------

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_ENV_FILES = (".claude.json", ".mcp.json")  # the Claude Code files that expand ${VAR}


def _env_slots(doc: Any, name: str) -> Iterable[tuple[tuple[str, ...], str, str, str]]:
    """Every string under mcpServers.<server>.env.<NAME> the way the Claude Code module reads
    them (the top level, and for ~/.claude.json each project's own servers):
    (JSON path, server name, variable name, value)."""
    if not isinstance(doc, dict):
        return
    holders: list[tuple[tuple[str, ...], Any]] = [((), doc)]
    projects = doc.get("projects") if name == ".claude.json" else None
    if isinstance(projects, dict):
        holders += [(("projects", k), v) for k, v in projects.items() if isinstance(k, str)]
    for prefix, holder in holders:
        servers = holder.get("mcpServers") if isinstance(holder, dict) else None
        for server, entry in servers.items() if isinstance(servers, dict) else ():
            env = entry.get("env") if isinstance(entry, dict) else None
            for var, value in env.items() if isinstance(env, dict) else ():
                if isinstance(server, str) and isinstance(value, str) and _ENV_NAME.fullmatch(var):
                    yield (*prefix, "mcpServers", server, "env", var), server, var, value


def _plain_json_string(value: str) -> bool:
    """True if the value is written in a JSON file exactly as itself between two quotes."""
    return value.isascii() and value.isprintable() and '"' not in value and "\\" not in value


def _with_value(doc: Any, path: tuple[str, ...], value: str) -> Any:
    out = copy.deepcopy(doc)
    node = out
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return out


def _occurrences(data: bytes, needle: bytes) -> Iterable[int]:
    start = data.find(needle)
    while start != -1:
        yield start
        start = data.find(needle, start + 1)


def _plan_env_file(path: Path, findings: list[Finding]) -> tuple[list[Change], list[Manual]]:
    """Replace every inline key under an env block of one Claude Code file with "${NAME}"."""
    changes: list[Change] = []
    var_of: dict[str, str] = {}  # finding detail -> variable that now holds the key
    data = _read(path)
    version = (0, 0)
    try:
        version = (path.stat().st_mtime_ns, path.stat().st_size)
        text_ok = data is not None and len(data) == version[1] and data.decode("utf-8") is not None
    except (OSError, UnicodeDecodeError):
        text_ok = False
    before = structured.parse(data, str(path)) if text_ok and data is not None else None
    if data is not None and before is not None and len(data) <= structured.MAX_BYTES:
        used: set[int] = set()
        slots = list(_env_slots(before, path.name))[:MAX_SETTINGS_TRIES]
        for jpath, server, var, value in slots:
            found = find_secrets(value)
            if len(found) != 1 or found[0].value != value or not _plain_json_string(value):
                continue
            needle = b'"' + value.encode() + b'"'
            for start in _occurrences(data, needle):
                if start in used:
                    continue
                new = data[:start] + f'"${{{var}}}"'.encode() + data[start + len(needle) :]
                if structured.parse(new, str(path)) != _with_value(before, jpath, "${" + var + "}"):
                    continue
                used.add(start)
                where = f"env.{var} of MCP server {safe_snippet(server, 60)!r}"
                changes.append(
                    Change(
                        kind="env",
                        path=path,
                        line=data.count(b"\n", 0, start) + 1,
                        check_id="AP005",
                        shown=f'{where}: key replaced by "${{{var}}}"',
                        offsets=((start, start + len(needle)),),
                        digests=(_digest(needle),),
                        json_path=json.dumps(list(jpath)),
                        var=var,
                        version=version,
                    )
                )
                field_text = safe_snippet(f"env.{var}", 60)
                detail = f"{found[0].name}: {redact(value)}{MCP_MARKER}"
                detail += f"{safe_snippet(server, 60)!r} ({field_text})"
                var_of[detail] = var
                break
    manual: list[Manual] = []
    for finding in findings:
        var = var_of.get(finding.detail)
        if var is None:
            manual += _leave_to_human([finding], False)[1]
            continue
        manual += _ask_human(finding, False)
        manual.append(
            Manual(
                finding.check_id,
                f"After rotating, set {var} in the shell that starts Claude Code to the new key: "
                f"the MCP config now reads it from there ({quote(str(path))}).",
            )
        )
    return changes, manual


def _plan_env(findings: list[Finding], pack: bool) -> tuple[list[Change], list[Manual]]:
    """AP005: Claude Code env entries become "${NAME}" references; every other entry (headers,
    args, URL, other agents) stays with the human and its per-agent advice."""
    changes: list[Change] = []
    manual: list[Manual] = []
    by_file: dict[str, list[Finding]] = {}
    for finding in findings:
        by_file.setdefault(finding.location, []).append(finding)
    for location, group in by_file.items():
        path = Path(location)
        ours = [f for f in group if f.agent == "claude-code"] if path.name in _ENV_FILES else []
        rest = [f for f in group if f not in ours]
        if ours:
            more_changes, more_manual = _plan_env_file(path, ours)
            changes += more_changes
            manual += more_manual
        manual += _leave_to_human(rest, pack)[1]
    return changes, manual


# ---- ignore lines: AP007 ---------------------------------------------------------------------


def _ignore_line(rel: str) -> str | None:
    """A .gitignore line that names exactly this file from the repo root, or None if the name
    can't be written as one line."""
    if "\n" in rel or "\r" in rel:
        return None
    line = re.sub(r"([\\*?\[\]])", r"\\\1", rel)
    trailing = len(line) - len(line.rstrip(" "))
    if trailing:
        line = line[:-trailing] + "\\ " * trailing
    return "/" + line


def _plan_ignore(findings: list[Finding], pack: bool) -> tuple[list[Change], list[Manual]]:
    """AP007: add the file to the repo's .gitignore. A file already committed is the human's:
    untracking it is a change to the repo's history."""
    changes: dict[tuple[Path, str], Change] = {}
    manual: list[Manual] = []
    for finding in findings:
        state = git_state(Path(finding.location))
        line = _ignore_line(state[1]) if state else None
        if state is None or line is None:
            manual += _leave_to_human([finding], pack)[1]
            continue
        top, rel, status = state
        if status == "tracked":
            steps = (
                f"{quote(rel)} is committed to git, so everyone who clones the repo has it. "
                f"In {quote(str(top))} run: git rm --cached -- {shlex.quote(rel)} ; then add "
                f"the line {quote(line)} to .gitignore and commit both. It stays in git history:"
            )
            manual.append(
                Manual(
                    finding.check_id,
                    f"{steps} ask the human to rotate any key it ever held.",
                    f"{steps} rotate any key it ever held.",
                )
            )
            continue
        target = top / ".gitignore"
        changes.setdefault(
            (target, line),
            Change(
                kind="ignore",
                path=target,
                line=None,
                check_id=finding.check_id,
                shown=f"add {safe_snippet(line, 200)} to {safe_snippet(str(target), 200)}",
                line_text=line,
                target=target,
            ),
        )
    return list(changes.values()), manual


# ---- file modes: AP008 -----------------------------------------------------------------------


def _plan_locks(findings: list[Finding], pack: bool) -> tuple[list[Change], list[Manual]]:
    """AP008: chmod go-rwx on exactly the files the check found (it hands them over on the
    finding; fix does not work them out again)."""
    changes: dict[Path, Change] = {}
    manual: list[Manual] = []
    if platform.is_windows():  # no go-rwx on Windows; the output says "not checked" (scan_notes)
        return [], _leave_to_human(findings, pack)[1] if findings else []
    for finding in findings:
        if not finding.paths:
            manual += _leave_to_human([finding], pack)[1]
            continue
        for path in finding.paths:
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode)
            except OSError:
                continue  # gone since the scan: nothing to lock
            if path not in changes and mode & 0o077:
                changes[path] = Change(
                    kind="lock",
                    path=path,
                    line=None,
                    check_id=finding.check_id,
                    shown=f"mode {mode:04o} to {mode & ~0o077:04o}",
                    old_mode=mode,
                )
    return list(changes.values()), manual


def _plan_rebuild(findings: list[Finding], pack: bool) -> tuple[list[Change], list[Manual]]:
    """AP009: fix never edits a build config, so each print is left to the human. A low print
    (an open-source package's maps, or a package for a private registry) is only a note."""
    manual = _leave_to_human(findings, pack)[1]
    return [], [
        replace(item, note=True) if finding.severity is Severity.LOW else item
        for finding, item in zip(findings, manual, strict=True)
    ]


PLANNERS.update(
    {"AP005": _plan_env, "AP007": _plan_ignore, "AP008": _plan_locks, "AP009": _plan_rebuild}
)


# ---- preflight: AP006 (npm packages) and AP002 inside packaged agent files ---------------------


def _npm_source(finding: Finding, root: Path | None) -> str | None:
    """The member name of a file in an npm project's package, or None when this is not a package
    fix can reach: a built archive, a Python package, or no project folder."""
    package, member = split_location(finding.location)
    if root is None or package is None or not package.endswith(".tgz"):
        return None
    if not (root / "package.json").is_file():
        return None
    return member


def _in_files_list(root: Path, member: str) -> bool:
    """True when package.json's "files" list would include this path (or can't be read)."""
    try:
        files = json.loads((root / "package.json").read_text(encoding="utf-8")).get("files")
    except (OSError, ValueError, AttributeError):
        return True  # cannot tell: do not guess
    if files is None:
        return False
    if not isinstance(files, list):
        return True
    for entry in files:
        if not isinstance(entry, str):
            return True
        pattern = entry.removeprefix("./").rstrip("/")
        if (
            member == pattern
            or member.startswith(pattern + "/")
            or fnmatch.fnmatchcase(member, pattern)
        ):
            return True
    return False


# Folders that also hold files a team commits and ships on purpose (workflows, shared agent
# config, commands, skills): an ignore line never names one of them whole.
SHARED_FOLDERS = frozenset(
    {".agents", ".claude", ".codex", ".copilot", ".cursor", ".gemini", ".github", ".vscode"}
)


def package_line(member: str) -> str | None:
    """The ignore line that keeps one agent file out of a package (and out of git), from the
    project root: the file itself (/.claude/settings.local.json), or the folder a package rule
    names when the rule is about a whole folder of agent data (/.codex/sessions/). Never a bare
    shared folder such as .claude/ or .github/, which would also drop files the team commits
    and ships on purpose. None if the name can't be written as one line."""
    for agent in ALL_AGENTS:
        for rule in agent.package_rules:
            match = rule.pattern.search(member)
            if match is None:
                continue
            folder = member[: match.end()].strip("/")
            whole_folder = match.end() < len(member) and member[match.end() - 1] == "/"
            if whole_folder and folder and folder not in SHARED_FOLDERS:
                line = _ignore_line(folder)
                return None if line is None else line + "/"
            return _ignore_line(member)
    return _ignore_line(member)


def _plan_package(findings: list[Finding], root: Path | None) -> tuple[list[Change], list[Manual]]:
    """AP006: keep the agent file out of an npm package (.npmignore) and out of git (.gitignore),
    with a line naming just that file (or the agent-data folder its package rule names). A
    "files" list is the human's; so is every package that is not a project folder with npm."""
    changes: dict[tuple[Path, str], Change] = {}
    manual: list[Manual] = []
    for finding in findings:
        member = _npm_source(finding, root)
        line = package_line(member) if member is not None else None
        if member is None or root is None or line is None:
            manual.append(_advice(finding, True))
            continue
        if _in_files_list(root, member):
            manual.append(
                Manual(
                    finding.check_id,
                    f'package.json has a "files" list that includes {quote(member)}, and '
                    "agentprint never edits package.json. Remove or narrow the matching entry "
                    f"so it no longer includes {quote(agent_path(member))}.",
                )
            )
        else:
            target = root / ".npmignore"
            changes.setdefault(
                (target, line),
                Change(
                    kind="ignore",
                    path=target,
                    line=None,
                    check_id=finding.check_id,
                    shown=f"add {safe_snippet(line, 200)} to {safe_snippet(str(target), 200)}",
                    line_text=line,
                    target=target,
                    seed=root / ".gitignore",
                ),
            )
        target = root / ".gitignore"
        changes.setdefault(
            (target, line),
            Change(
                kind="ignore",
                path=target,
                line=None,
                check_id=finding.check_id,
                shown=f"add {safe_snippet(line, 200)} to {safe_snippet(str(target), 200)}",
                line_text=line,
                target=target,
            ),
        )
    # .npmignore first: it starts as a copy of .gitignore as it is before fix touches it.
    ordered = sorted(changes.values(), key=lambda c: c.path.name != ".npmignore")
    return ordered, manual


def _plan_packaged_secrets(
    findings: list[Finding], root: Path | None
) -> tuple[list[Change], list[Manual]]:
    """AP002 inside a packaged agent file: the package copy is built from the project file, so
    mask there (same rules as for a scan) and never touch an archive."""
    changes: list[Change] = []
    manual: list[Manual] = []
    for finding in findings:
        member = _npm_source(finding, root)
        source = root / member if root is not None and member is not None else None
        if source is None or ".." in Path(member or "").parts or not source.is_file():
            manual.append(_advice(finding, True))
            continue
        more_changes, more_manual = _plan_masks([replace(finding, location=str(source))], True)
        changes += more_changes
        manual += more_manual
    merged: dict[tuple[str, Path, int | None], Change] = {}
    for change in changes:  # one key in two findings of one file: planned once
        merged.setdefault((change.kind, change.path, change.offsets[0][0]), change)
    return list(merged.values()), manual


# ---- applying --------------------------------------------------------------------------------


class Journal:
    """~/.agentprint/fix/<UTC stamp>.json: every change one run made (0600, folder 0700).

    Each entry: kind, path, line, check id, masked stub (`shown`) and `undo` (what --undo needs;
    null when the change can't be undone, such as a masked key). Never holds a key. The file
    is created at the first recorded change; a stamp already taken moves on by a second.
    `close()` removes it again if no change was recorded: an empty newest journal would hide
    the run before it from --undo.

    It also remembers this run's own in-place writes (`wrote`), so a later change to the same
    file can tell agentprint's own edit from someone else's."""

    def __init__(self, home: Path, now: datetime | None = None) -> None:
        self.folder = home / JOURNAL_DIR
        self.started = now or datetime.now(UTC)
        self.path: Path | None = None
        self.entries: list[dict[str, Any]] = []
        self.saved = 0  # how many entries the file on disk holds
        self.written: dict[Path, tuple[Version, Version]] = {}

    @property
    def backups(self) -> Path:
        """This run's folder of ignore-file copies."""
        return self.folder / "backups" / self.stamp

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
        self.saved = len(self.entries)

    def wrote(self, path: Path, before: Version, after: Version) -> None:
        """This run rewrote bytes of `path` in place: it was at `before`, it is at `after`."""
        self.written[path] = (before, after)

    def as_planned(self, path: Path, planned: Version | None, now: Version) -> bool:
        """True if the file is still as planned, or as this run's own writes left a file that
        was as planned when they began (masks keep a file's size; the caller still checks the
        bytes it changes)."""
        return now == planned or (planned is not None and self.written.get(path) == (planned, now))

    def close(self) -> None:
        """A run that recorded no change leaves no journal, and no empty backup folders."""
        if self.path is None or self.saved:
            return
        with contextlib.suppress(OSError):
            self.path.unlink()
        self.path = None
        # Only empty folders go: a backup is deleted as soon as its write does not happen.
        folders = [Path(top) for top, _, _ in os.walk(self.backups, topdown=False)]
        for folder in [*folders, self.backups.parent]:
            with contextlib.suppress(OSError):
                folder.rmdir()


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
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
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
        if any(r.reason is None for r in results):  # an env change to this file may follow
            after = os.fstat(fd)
            journal.wrote(path, (st.st_mtime_ns, st.st_size), (after.st_mtime_ns, after.st_size))
        return results
    except OSError as exc:
        return [Result(c, _cannot_write(exc)) for c in changes]
    finally:
        os.close(fd)


def _stat_version(path: Path) -> Version | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _apply_env(changes: list[Change], journal: Journal) -> list[Result]:
    """Replace the planned values with "${NAME}". Refuses if the file's mtime or size moved or
    a value is no longer there; checks that the new file parses and differs from the old one only
    at those values; then replaces the file atomically (same mode). No backup: it held a key."""
    path = changes[0].path
    name = str(path)
    bad = "agentprint could not make this change safely; left as is"
    version = _stat_version(path)
    if version is None:
        return [Result(c, GONE) for c in changes]
    if not journal.as_planned(path, changes[0].version, version):  # masks may have come first
        return [Result(c, CHANGED) for c in changes]
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [Result(c, _cannot_write(exc)) for c in changes]
    if len(data) != version[1] or any(
        _digest(data[s:e]) != d
        for c in changes
        for (s, e), d in zip(c.offsets, c.digests, strict=True)
    ):
        return [Result(c, CHANGED) for c in changes]
    before = structured.parse(data, name)
    expected, new = before, data
    try:
        for c in sorted(changes, key=lambda c: c.offsets[0][0], reverse=True):
            (start, end), reference = c.offsets[0], "${" + c.var + "}"
            new = new[:start] + json.dumps(reference).encode() + new[end:]
            expected = _with_value(expected, tuple(json.loads(c.json_path)), reference)
        text = new.decode("utf-8")
    except (KeyError, TypeError, ValueError, IndexError):
        return [Result(c, bad) for c in changes]
    if before is None or structured.parse(new, name) != expected:
        return [Result(c, bad) for c in changes]
    if _stat_version(path) != version:  # one last look before replacing the file
        return [Result(c, CHANGED) for c in changes]
    try:
        atomic_write(path, text)
    except OSError as exc:
        return [Result(c, _cannot_write(exc)) for c in changes]
    return [Result(c) for c in changes]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _apply_ignore(changes: list[Change], journal: Journal) -> list[Result]:
    """Add the planned lines to one ignore file: a new file if there is none; otherwise the old
    one is first copied to ~/.agentprint/fix/backups/<stamp>/ (0600, folders 0700). Lines that are
    already there are not added twice."""
    path = changes[0].path
    target = real(path)
    try:
        exists = os.path.lexists(target)
        if exists and (
            not stat.S_ISREG(target.stat().st_mode) or target.stat().st_size > MAX_BYTES
        ):
            return [Result(c, "the ignore file is not a plain file; left as is") for c in changes]
        version = _stat_version(target)
        old = target.read_bytes() if exists else b""
        seeded = False
        seed = changes[0].seed
        if not exists and seed is not None and os.path.isfile(seed):
            if os.stat(seed).st_size > MAX_BYTES:
                return [
                    Result(c, "the file to copy from is too large; left as is") for c in changes
                ]
            old, seeded = Path(seed).read_bytes(), True
        present = {line.rstrip(b"\r") for line in old.split(b"\n")}
        eol = b"\r\n" if b"\r\n" in old else b"\n"
        new = old + (eol if old and not old.endswith(b"\n") else b"")
        results: list[Result] = []
        added: list[Result] = []
        for change in changes:
            line = change.line_text.encode("utf-8")
            if line in present:
                results.append(Result(change, "the line is already in the ignore file"))
                continue
            present.add(line)
            new += line + eol
            added.append(Result(change))
        if seeded and not added:  # the copy already holds every line; it is still the new file
            added, results = results, []
            for result in added:
                result.reason = None
        if not added:
            return results
        text = new.decode("utf-8")
        undo: dict[str, Any] = {"target": str(target), "after": _sha(new), "op": "delete"}
        if _stat_version(target) != version:
            return [Result(c, CHANGED) for c in changes]
        backup: Path | None = None
        written = False
        try:
            if exists:
                journal.open()  # claims the run's stamp, which names its backup folder
                copy = journal.backups / Path(*platform.drive_safe_parts(target.resolve()))
                copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                private_dir(journal.folder / "backups")
                fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                backup = copy  # created here: removed again if the write does not happen
                with os.fdopen(fd, "wb") as fh:
                    fh.write(old)
                undo["op"], undo["backup"] = "restore", str(backup)
            if _stat_version(target) != version:  # one last look before writing
                return [Result(c, CHANGED) for c in changes]
            atomic_write(target, text, 0o644)
            written = True
        finally:
            if backup is not None and not written:  # nothing to restore: no copy left behind
                with contextlib.suppress(OSError):
                    backup.unlink()
    except UnicodeDecodeError:
        return [Result(c, "the ignore file is not UTF-8 text; left as is") for c in changes]
    except OSError as exc:
        return [Result(c, _cannot_write(exc)) for c in changes]
    for result in added:
        result.undo = undo
    return results + added


def _apply_lock(changes: list[Change], journal: Journal) -> list[Result]:
    """chmod go-rwx on a file (the owner's bits, execute included, are kept) and journal the
    mode it had right now."""
    results = []
    for change in changes:
        try:
            st = os.stat(change.path)
            if not stat.S_ISREG(st.st_mode):
                results.append(Result(change, CHANGED))
                continue
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                results.append(Result(change, "the file belongs to another user; left as is"))
                continue
            old = stat.S_IMODE(st.st_mode)
            new = old & ~0o077
            if new == old:
                results.append(Result(change, "the file is already private"))
                continue
            os.chmod(change.path, new)
            results.append(Result(change, None, {"mode": old, "now": new}))
        except FileNotFoundError:
            results.append(Result(change, GONE))
        except OSError as exc:
            results.append(Result(change, _cannot_write(exc)))
    return results


APPLIERS: dict[str, Applier] = {
    "mask": _apply_masks,
    "env": _apply_env,
    "ignore": _apply_ignore,
    "lock": _apply_lock,
}


def apply(p: Plan, home: Path) -> Outcome:
    """Make the plan's changes, file by file, journaling each one under `home`. A change whose
    file changed or vanished since the plan is skipped with a reason; the others go ahead."""
    journal = Journal(home)
    applied: list[Change] = []
    skipped: list[tuple[Change, str]] = []
    groups: dict[tuple[str, Path], list[Change]] = {}
    for change in p.changes:
        groups.setdefault((change.kind, change.path), []).append(change)
    # Masks keep a file's byte offsets, an env rewrite does not: in a file that gets both, the
    # masks go first and the env change accepts the file as they left it.
    order = list(groups)
    for key in [k for k in order if k[0] == "env" and ("mask", k[1]) in groups]:
        order.remove(key)
        order.insert(order.index(("mask", key[1])) + 1, key)
    stop: str | None = None  # set when the journal can't be written: nothing more is changed
    try:
        for kind, path in order:
            changes = groups[(kind, path)]
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
        journal.close()
    return Outcome(applied, skipped, journal.path)


# ---- undo ------------------------------------------------------------------------------------

_JOURNAL_NAME = re.compile(r"\d{8}T\d{6}Z\.json")
NOTHING_TO_UNDO = "There is nothing to undo"  # how undo()'s answer starts when there is no journal
_KEY_GONE = {
    "mask": "a masked key in {path} can't be put back: the key is gone, by design",
    "env": "the key that was in {path} ({shown}) can't be put back: the key is gone, by design",
}


def _undo_entry(entry: dict[str, Any], folder: Path, done: set[tuple[str, str]]) -> str | None:
    """Revert one journal entry. Returns None when it was reverted, else why it was not."""
    kind, path = str(entry.get("kind")), str(entry.get("path"))
    undo = entry.get("undo")
    if not isinstance(undo, dict):
        text = _KEY_GONE.get(kind, "this {kind} change can't be undone")
        return text.format(path=quote(path), shown=quote(str(entry.get("shown"))), kind=kind)
    if kind == "lock":
        target = Path(path)
        mode = stat.S_IMODE(os.stat(target).st_mode)
        if mode != undo["now"]:
            return f"the mode of {quote(path)} was changed again since fix; left as it is now"
        os.chmod(target, undo["mode"])
        return None
    if kind == "ignore":
        target = Path(undo["target"])
        if (undo["op"], str(target)) in done:
            return None  # one restore covers every line fix added to this file
        if _sha(target.read_bytes()) != undo["after"]:
            return f"{quote(str(target))} was edited since fix; remove the added line by hand"
        if undo["op"] == "delete":
            target.unlink()
        else:
            backup = Path(undo["backup"]).resolve()
            if (folder / "backups").resolve() not in backup.parents:
                return "the journal names a backup outside its own folder; left as is"
            atomic_write(target, backup.read_bytes().decode("utf-8"))
        done.add((undo["op"], str(target)))
        return None
    return f"this {kind} change can't be undone"


def undo(home: Path) -> tuple[list[str], list[str]]:
    """Revert what the newest fix journal can: file modes and ignore files. Returns (restored,
    not restorable), both plain sentences. Masked keys and env references are gone for good.
    Older journals are left alone. With no journal there is nothing to do, and it says so."""
    folder = home / JOURNAL_DIR
    names = sorted(p for p in folder.glob("*.json") if _JOURNAL_NAME.fullmatch(p.name))
    if not names:
        return [], [f"{NOTHING_TO_UNDO}: no fix journal in {quote(str(folder))}."]
    path = names[-1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = [e for e in data["changes"] if isinstance(e, dict)]
    except (OSError, ValueError, KeyError, TypeError):
        return [], [f"The newest fix journal ({path.name}) can't be read, so nothing was undone."]
    restored: list[str] = []
    cannot: list[str] = []
    done: set[tuple[str, str]] = set()
    for entry in entries:
        if entry.get("undone"):
            continue
        what = {"lock": "mode of", "ignore": "ignore file"}.get(str(entry.get("kind")), "")
        where = quote(str(entry.get("path")))
        try:
            reason = _undo_entry(entry, folder, done)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            reason = f"{where} could not be put back ({type(exc).__name__}); left as is"
        if reason is None:
            entry["undone"] = True
            restored.append(f"restored the {what} {where}".replace("the  ", "the "))
        else:
            cannot.append(reason)
    if restored:
        with contextlib.suppress(OSError):
            atomic_write(path, json.dumps(data, indent=2) + "\n", 0o600)
    return restored, cannot
