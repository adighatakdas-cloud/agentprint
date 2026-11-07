"""Accepted findings: the baseline file, fingerprints, and which findings still count.

A fingerprint is the sha256 of "check id|path|rule|stub": the file's path from the project root
with forward slashes (`~/...` for a home-folder file, the name inside the package for a package
file), the key rule's name (the check's title for a finding that is not a key) and the redact()
stub, the key's first 4 characters and its length. Never the key, a hash of the key or a hash of
the file's text, and no line number (lines move). A baseline file therefore holds nothing a key
can be read back from: check ids, paths, reasons, counts and these hashes.

Findings an ignore rule or the baseline covers are "known": shown apart, counted, and they never
fail a run. Ruling C1: a key that would ship in a package being published, or in a push, always
counts. Neither an ignore rule nor the baseline makes it known, and a fail_on set in the config
cannot let it through. Both still apply to the other findings of a publish or push.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from . import __version__
from .config import Config, Sources
from .config import load as load_config
from .models import Finding
from .report import FailOn, exit_code
from .sanitize import clean, quote

FORMAT = 1
MAX_BASELINE_BYTES = 8 * 1024 * 1024
KEY_CHECKS = frozenset({"AP001", "AP002", "AP005"})  # findings whose detail is a redacted key
STUB = re.compile(r"\S{1,4}…\(len \d+\)")  # what secrets.redact() prints
FINGERPRINT = re.compile(r"[0-9a-f]{64}")
NOTE = (
    "Findings this project accepted: agentprint counts them as known, not as problems. "
    "Fingerprints only: no key is stored here, nor anything a key can be read back from. "
    "A key in a package being published or in a push still blocks. "
    "Rewrite with: agentprint baseline"
)


def today() -> str:
    return datetime.date.today().isoformat()


@dataclass(frozen=True)
class Entry:
    fingerprint: str
    check: str
    path: str
    reason: str
    count: int = 1  # findings with this fingerprint that were accepted


@dataclass(frozen=True)
class Known:
    """A finding that does not count: the baseline or an ignore rule covers it."""

    finding: Finding
    via: str  # "baseline" or "ignore"
    reason: str
    path: str  # as matched: from the project root, ~/..., or the name inside the package


@dataclass(frozen=True)
class Project:
    config: Config
    entries: Mapping[str, Entry]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settled:
    active: list[Finding]  # what counts: rendered, exit code, badge
    known: list[Known]
    fail_on: FailOn


def load(root: Path, sources: Sources | None = None) -> Project:
    """The project's config and baseline. Never raises: problems become warnings."""
    config = load_config(root, sources)
    if config.baseline is None:
        return Project(config, {}, config.warnings)
    entries, warnings = read(config.baseline)
    return Project(config, entries, config.warnings + tuple(warnings))


def defaults(root: Path) -> Project:
    """No config, no baseline: every finding counts."""
    return Project(Config(root), {})


def read(path: Path) -> tuple[dict[str, Entry], list[str]]:
    """The entries of a baseline file. No file: none, and no warning."""
    name = clean(str(path))
    unusable = f"could not read baseline {name}"
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_BASELINE_BYTES + 1)
    except FileNotFoundError:
        return {}, []
    except OSError as exc:
        return {}, [f"{unusable}: {clean(exc.strerror or type(exc).__name__)}"]
    if len(raw) > MAX_BASELINE_BYTES:
        return {}, [f"{unusable}: larger than 8 MiB"]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {}, [f"{unusable}: {clean(str(exc))}"]
    items = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}, [f"{unusable}: no entries list"]
    entries: dict[str, Entry] = {}
    bad = 0
    for item in items:
        fp = item.get("fingerprint") if isinstance(item, dict) else None
        count = item.get("count", 1) if isinstance(item, dict) else 0
        if not (isinstance(fp, str) and FINGERPRINT.fullmatch(fp)) or not _count_ok(count):
            bad += 1
            continue
        assert isinstance(item, dict)
        entries[fp] = Entry(
            fp,
            clean(str(item.get("check", "")), 20),
            clean(str(item.get("path", ""))),
            clean(str(item.get("reason", "")), 200) or "accepted",
            count,
        )
    warnings = [f"{bad} entries in baseline {name} are not valid and were skipped"] if bad else []
    return entries, warnings


def _count_ok(count: object) -> bool:
    return isinstance(count, int) and not isinstance(count, bool) and count >= 1


def dumps(entries: Iterable[Entry]) -> str:
    """The baseline file: sorted and stable, so it diffs cleanly."""
    rows = sorted(entries, key=lambda e: (e.path, e.check, e.fingerprint))
    data = {
        "tool": "agentprint",
        "format": FORMAT,
        "note": NOTE,
        "entries": [
            {
                "fingerprint": e.fingerprint,
                "check": e.check,
                "path": e.path,
                "reason": e.reason,
                "count": e.count,
            }
            for e in rows
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def is_key(finding: Finding) -> bool:
    return finding.check_id in KEY_CHECKS


def _inside(location: str, folder: Path) -> str | None:
    """`location` from `folder`, with forward slashes, or None when it is not inside it. As
    spelled first, then with links resolved (/var and /private/var on macOS)."""
    for spell in (os.path.abspath, os.path.realpath):
        try:
            return Path(spell(location)).relative_to(spell(folder)).as_posix()
        except (ValueError, OSError):  # not inside; a drive apart; an unreadable link
            continue
    return None


def where(
    finding: Finding, root: Path, home: Path | None, package: bool | None = False
) -> tuple[str, bool]:
    """The path a finding is matched by, and whether it is the project's own file.

    `package` True: the finding is inside a package ("label!name"); None: guess (a publish
    guard mixes package findings with a push's absolute paths)."""
    location = finding.location
    if package is None:
        package = "!" in location and not os.path.isabs(location)
    if package:
        return location.partition("!")[2] or location, True
    inside = _inside(location, root)
    if inside is not None:
        return inside, True
    if home is not None and (mine := _inside(location, home)) is not None:
        return "~" if mine == "." else f"~/{mine}", False
    return Path(os.path.abspath(location)).as_posix(), False


def fingerprint(finding: Finding, path: str) -> str:
    """sha256 of check id, path, rule name (or check title) and the redact() stub."""
    stub = STUB.search(finding.detail) if is_key(finding) else None
    if stub is not None:
        rule = finding.detail[: stub.start()].rstrip().removesuffix(":")
        text = "|".join((finding.check_id, path, rule, stub.group()))
    else:
        text = "|".join((finding.check_id, path, finding.title, ""))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def settle(
    findings: list[Finding],
    project: Project,
    fail_on: FailOn | None,
    *,
    publishing: bool,
    package: bool | None = False,
    home: Path | None = None,
) -> Settled:
    """Split findings into what counts and what is known, and pick the fail_on.

    `fail_on` is the command line's (None: not given, so the config's, else high).
    `publishing`: a package publish or a push, where Ruling C1 holds."""
    config = project.config
    left = {fp: entry.count for fp, entry in project.entries.items()}
    active: list[Finding] = []
    known: list[Known] = []
    for f in findings:
        if publishing and is_key(f):  # Ruling C1: a key on its way out always counts
            active.append(f)
            continue
        path, own = where(f, config.root, home, package)
        rule = next((r for r in config.ignore if r.matches(f.check_id, path)), None)
        if own and rule is not None:
            known.append(Known(f, "ignore", rule.reason, path))
            continue
        fp = fingerprint(f, path)
        if left.get(fp, 0) > 0:
            left[fp] -= 1
            known.append(Known(f, "baseline", project.entries[fp].reason, path))
            continue
        active.append(f)
    return Settled(active, known, resolve_fail_on(fail_on, config, active, publishing))


def resolve_fail_on(
    given: FailOn | None, config: Config, active: list[Finding], publishing: bool
) -> FailOn:
    """The command line's --fail-on, else the config's, else high. In a publish or push a
    config's fail_on never lets a key through (Ruling C1)."""
    if given is not None:
        return given
    chosen = config.fail_on or FailOn.high
    keys = [f for f in active if is_key(f)] if publishing else []
    if any(not exit_code([k], chosen) for k in keys):
        chosen = FailOn(min(k.severity for k in keys).name.lower())
    return chosen


# ---- `agentprint baseline` ---------------------------------------------------------------------


def tally(findings: Iterable[Finding], root: Path, home: Path | None) -> dict[str, Entry]:
    """The findings as baseline entries (reason still empty), one per fingerprint."""
    out: dict[str, Entry] = {}
    for f in findings:
        path, _ = where(f, root, home)
        fp = fingerprint(f, path)
        seen = out.get(fp)
        out[fp] = Entry(fp, f.check_id, clean(path), "", seen.count + 1 if seen else 1)
    return out


@dataclass(frozen=True)
class Rewrite:
    entries: list[Entry]
    added: int
    kept: int
    dropped: int


def rewrite(old: Mapping[str, Entry], now: Mapping[str, Entry], mode: str, reason: str) -> Rewrite:
    """The new baseline. "write": today's findings (old reasons kept); "update": old entries
    plus today's new findings; "prune": old entries whose finding is still there."""
    if mode == "update":
        entries = dict(old)
        for fp, e in now.items():
            entries[fp] = (
                replace(old[fp], count=max(old[fp].count, e.count))
                if fp in old
                else replace(e, reason=reason)
            )
    elif mode == "prune":
        entries = {
            fp: replace(e, count=min(e.count, now[fp].count)) for fp, e in old.items() if fp in now
        }
    else:
        entries = {
            fp: replace(e, reason=old[fp].reason if fp in old else reason) for fp, e in now.items()
        }
    return Rewrite(
        list(entries.values()),
        added=len(entries.keys() - old.keys()),
        kept=len(entries.keys() & old.keys()),
        dropped=len(old.keys() - entries.keys()),
    )


@dataclass(frozen=True)
class Check:
    entries: int
    matched: int
    stale: list[Entry]  # entries whose finding is gone: safe to drop
    new: int  # findings the baseline does not cover


def check(old: Mapping[str, Entry], now: Mapping[str, Entry]) -> Check:
    stale = [e for fp, e in old.items() if fp not in now]
    new = sum(max(0, e.count - (old[fp].count if fp in old else 0)) for fp, e in now.items())
    stale.sort(key=lambda e: (e.path, e.check, e.fingerprint))
    return Check(len(old), len(old) - len(stale), stale, new)


# ---- what `agentprint baseline` says -----------------------------------------------------------


def _n(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _keys(entries: Iterable[Entry]) -> int:
    return sum(e.count for e in entries if e.check in KEY_CHECKS)


def _home(entries: Iterable[Entry]) -> int:
    return sum(1 for e in entries if e.path.startswith("~"))


def asks(change: Rewrite, file: str, person: bool = False) -> list[str]:
    """What only the human can decide about a written baseline: for an AI agent to ask, or
    (`person`) said to the person who ran it."""
    review = "review it before you commit it" if person else f"ask the human to review {file}"
    out = [
        f"{review[0].upper()}{review[1:]}: it accepts these findings for everyone who works on "
        "this project."
    ]
    if keys := _keys(change.entries):
        rotate = "Rotate each one" if person else "Ask the human to rotate each one"
        out.append(
            f"{_n(keys, 'accepted finding is a key', 'accepted findings are keys')}. The "
            "baseline only keeps a key from failing a scan: it does not make it safe, and a key "
            f"in a publish or push still blocks. {rotate} at its provider."
        )
    if home := _home(change.entries):
        out.append(
            f"{_n(home, 'entry is', 'entries are')} from the home folder, which is personal: keep "
            f"{'this file' if person else file} out of git, or write it without --include-home."
        )
    return out


def _counts(change: Rewrite, sep: str) -> str:
    return sep.join((f"{change.added} added", f"{change.kept} kept", f"{change.dropped} dropped"))


def to_agent_write(change: Rewrite, file: str, mode: str) -> str:
    verb = {"update": "updated", "prune": "pruned"}.get(mode, "wrote")
    entries = _n(len(change.entries), "entry", "entries")
    out = [
        f"agentprint baseline: {verb} {quote(file)} — {entries} ({_counts(change, ', ')})",
        "These findings now count as known: agentprint scan and fix list them apart and do not "
        "fail on them. The file holds fingerprints only, never a key.",
        "ask_human:",
        *(f"  - {step}" for step in asks(change, quote(file))),
        "Tell the human each ask_human item.",
    ]
    return "\n".join(out)


def to_human_write(change: Rewrite) -> list[str]:
    """The person's view (cli prints it): counts, then what is theirs to decide."""
    entries = _n(len(change.entries), "entry", "entries")
    return [f"{entries} · {_counts(change, ' · ')}", *asks(change, "", person=True)]


def to_agent_check(result: Check, file: str) -> str:
    if not result.entries:
        head = f"agentprint baseline --check: no entries in {quote(file)}"
    else:
        head = (
            f"agentprint baseline --check: {_n(result.entries, 'entry', 'entries')} in "
            f"{quote(file)}, {result.matched} still found, {len(result.stale)} stale"
        )
    head += f", {_n(result.new, 'new finding')} not in the baseline"
    out = [head]
    if result.stale:
        out.append("stale (the finding is gone, so the entry can be dropped):")
        out += [f"  - {e.check} {quote(e.path)} {quote(e.reason)}" for e in result.stale]
    last = ["Nothing was changed."]
    if result.stale:
        last.append("To drop the stale entries: agentprint baseline --prune.")
    if result.new:
        last.append("To see the new findings: agentprint scan.")
    out.append(" ".join(last))
    return "\n".join(out)


def to_json_write(change: Rewrite, file: str, mode: str) -> str:
    data = {
        "tool": "agentprint",
        "version": __version__,
        "baseline": clean(file),
        "mode": mode,
        "entries": len(change.entries),
        "added": change.added,
        "kept": change.kept,
        "dropped": change.dropped,
        "ask_human": asks(change, clean(file)),
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def to_json_check(result: Check, file: str) -> str:
    data = {
        "tool": "agentprint",
        "version": __version__,
        "baseline": clean(file),
        "mode": "check",
        "entries": result.entries,
        "matched": result.matched,
        "stale": [{"check_id": e.check, "path": e.path, "reason": e.reason} for e in result.stale],
        "new": result.new,
    }
    return json.dumps(data, indent=2, ensure_ascii=False)
