"""Accepted findings: the baseline file, fingerprints, and which findings still count.

A fingerprint is the sha256 of "check id|path|rule|stub": the file's path from the project root
with forward slashes (`~/...` for a home-folder file, the name inside the package for a package
file), the key rule's name (for a finding that is not a key, what it is about, else the check's
title) and the redact() stub, the key's first 4 characters and its length. Never the key, a hash
of the key or a hash of the file's text, and no line number (lines move). A baseline file
therefore holds nothing a key can be read back from: check ids, paths, reasons, counts and these
hashes.

Findings an ignore rule or the baseline covers are "known": shown apart, counted, and they never
fail a run. A project's ignore rules and its baseline (files in the repository, written by
whoever wrote it) only ever cover the project's own files. A finding in your home folder can
only be accepted by your personal baseline, `<home>/.agentprint/baseline.json` (`agentprint
baseline --include-home` writes it), or by a --baseline file you name outside the project.

Ruling C1: a key that would ship in a package being published, or in a push, always counts.
Neither an ignore rule nor the baseline makes it known, and a fail_on set in the config cannot
let it through. Both still apply to the other findings of a publish or push.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import __version__
from .config import Config, Sources, read_regular
from .config import load as load_config
from .models import Finding
from .report import FailOn, exit_code
from .sanitize import clean, quote

FORMAT = 1
MAX_BASELINE_BYTES = 8 * 1024 * 1024
KEY_CHECKS = frozenset({"AP001", "AP002", "AP005"})  # findings whose detail is a redacted key
STUB = re.compile(r"\S{1,4}…\(len \d+\)")  # what secrets.redact() prints
FINGERPRINT = re.compile(r"[0-9a-f]{64}")
PERSONAL = Path(".agentprint") / "baseline.json"  # under the home folder: yours alone
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
    via: str  # "baseline", "personal" (your own baseline) or "ignore"
    reason: str
    path: str  # as matched: from the project root, ~/..., or the name inside the package


@dataclass(frozen=True)
class Project:
    config: Config
    entries: Mapping[str, Entry]  # the baseline file: covers the project's own files only...
    warnings: tuple[str, ...] = ()
    personal: Mapping[str, Entry] = field(default_factory=dict)  # yours: home-folder files
    trusted: bool = False  # ...unless --baseline named a file outside the project: then any


@dataclass(frozen=True)
class Settled:
    active: list[Finding]  # what counts: rendered, exit code, badge
    known: list[Known]
    fail_on: FailOn


def load(root: Path, sources: Sources | None = None, home: Path | None = None) -> Project:
    """The project's config and baseline, and with `home` (a sweep of that home folder) your
    personal baseline in it. Never raises: problems become warnings. --no-config reads none of
    them unless --baseline names a file."""
    sources = sources or Sources()
    config = load_config(root, sources)
    warnings = list(config.warnings)
    entries: dict[str, Entry] = {}
    if config.baseline is not None:  # the default or the config's: never through a link
        entries, more = read(config.baseline, links=sources.baseline is not None)
        warnings += more
    named = sources.baseline
    trusted = named is not None and not _within(named, root)
    personal: dict[str, Entry] = {}
    if home is not None and not sources.off:
        personal, more = read(home / PERSONAL)
        warnings += more
    return Project(config, entries, tuple(warnings), personal, trusted)


def _within(path: Path, folder: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(folder))
    except (OSError, ValueError):
        return False


def defaults(root: Path) -> Project:
    """No config, no baseline: every finding counts."""
    return Project(Config(root), {})


def read(path: Path, links: bool = True) -> tuple[dict[str, Entry], list[str]]:
    """The entries of a baseline file: a regular file, read without ever blocking, at most 8
    MiB, and with `links` False never through a symbolic link. No file: none, no warning."""
    name = clean(str(path))
    unusable = f"could not read baseline {name}"
    try:
        raw = read_regular(path, MAX_BASELINE_BYTES, links=links)
    except FileNotFoundError:
        return {}, []
    except OSError as exc:
        return {}, [f"{unusable}: {clean(exc.strerror or type(exc).__name__)}"]
    except ValueError as exc:
        return {}, [f"{unusable}: {clean(str(exc))}"]
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
    """sha256 of check id, path, rule name (for a finding that is not a key, its subject or the
    check's title) and the redact() stub."""
    stub = STUB.search(finding.detail) if is_key(finding) else None
    if stub is not None:
        rule = finding.detail[: stub.start()].rstrip().removesuffix(":")
        text = "|".join((finding.check_id, path, rule, stub.group()))
    else:  # what it is about (AP003 setting + value, AP004 event + indicators), else the title
        text = "|".join((finding.check_id, path, finding.subject or finding.title, ""))
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
    mine = {fp: entry.count for fp, entry in project.personal.items()}
    active: list[Finding] = []
    known: list[Known] = []
    for f in findings:
        if publishing and is_key(f):  # Ruling C1, decided before any config rule
            active.append(f)
            continue
        path, own = where(f, config.root, home, package)
        if own:  # the project's own file: its ignore rules, then its baseline
            rule = next((r for r in config.ignore if r.matches(f.check_id, path)), None)
            if rule is not None:
                known.append(Known(f, "ignore", rule.reason, path))
                continue
        fp = fingerprint(f, path)
        if (own or project.trusted) and _take(left, fp):
            known.append(Known(f, "baseline", project.entries[fp].reason, path))
        elif path.startswith("~") and _take(mine, fp):  # never a repository's say-so
            known.append(Known(f, "personal", project.personal[fp].reason, path))
        else:
            active.append(f)
    return Settled(active, known, resolve_fail_on(fail_on, config, active, publishing))


def _take(left: dict[str, int], fp: str) -> bool:
    """Use up one acceptance of `fp`, if any is left."""
    if left.get(fp, 0) <= 0:
        return False
    left[fp] -= 1
    return True


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


def tally(
    findings: Iterable[Finding], root: Path, home: Path | None, *, personal: bool = False
) -> dict[str, Entry]:
    """The project's own findings (`personal`: the home folder's instead) as baseline entries,
    reason still empty, one per fingerprint."""
    out: dict[str, Entry] = {}
    for f in findings:
        path, own = where(f, root, home)
        wanted = (not own and path.startswith("~")) if personal else own
        if not wanted:
            continue
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


@dataclass(frozen=True)
class Written:
    file: str  # as shown
    change: Rewrite
    personal: bool = False  # your personal baseline (home-folder findings), not the project's


@dataclass(frozen=True)
class Checked:
    file: str
    result: Check
    personal: bool = False


def _n(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _keys(entries: Iterable[Entry]) -> int:
    return sum(e.count for e in entries if e.check in KEY_CHECKS)


def asks(written: list[Written], person: bool = False) -> list[str]:
    """What only the human can decide about a written baseline: for an AI agent to ask, or
    (`person`) said to the person who ran it. The project's file comes first."""
    file = quote(written[0].file)
    review = "review it before you commit it" if person else f"ask the human to review {file}"
    out = [
        f"{review[0].upper()}{review[1:]}: it accepts these findings for everyone who works on "
        "this project."
    ]
    if keys := sum(_keys(w.change.entries) for w in written):
        rotate = "Rotate each one" if person else "Ask the human to rotate each one"
        out.append(
            f"{_n(keys, 'accepted finding is a key', 'accepted findings are keys')}. The "
            "baseline only keeps a key from failing a scan: it does not make it safe, and a key "
            f"in a publish or push still blocks. {rotate} at its provider."
        )
    return out


def _counts(change: Rewrite, sep: str) -> str:
    return sep.join((f"{change.added} added", f"{change.kept} kept", f"{change.dropped} dropped"))


PERSONAL_NOTE = "your home folder's findings, for you alone: no repository can add to it"


def to_agent_write(written: list[Written], mode: str) -> str:
    verb = {"update": "updated", "prune": "pruned"}.get(mode, "wrote")
    lines = []
    for w in written:
        entries = _n(len(w.change.entries), "entry", "entries")
        line = f"{verb} {quote(w.file)} — {entries} ({_counts(w.change, ', ')})"
        lines.append(f"personal: {line}; {PERSONAL_NOTE}" if w.personal else line)
    out = [
        f"agentprint baseline: {lines[0]}",
        *lines[1:],
        "These findings now count as known: agentprint scan and fix list them apart and do not "
        "fail on them. The file holds fingerprints only, never a key.",
        "ask_human:",
        *(f"  - {step}" for step in asks(written)),
        "Tell the human each ask_human item.",
    ]
    return "\n".join(out)


def to_human_write(written: list[Written]) -> list[str]:
    """The person's view (cli prints it): counts per file, then what is theirs to decide."""
    out = []
    for w in written:
        entries = _n(len(w.change.entries), "entry", "entries")
        where = f"personal {w.file} ({PERSONAL_NOTE}): " if w.personal else ""
        out.append(f"{where}{entries} · {_counts(w.change, ' · ')}")
    return [*out, *asks(written, person=True)]


def _check_line(c: Checked) -> str:
    r = c.result
    if not r.entries:
        return f"no entries in {quote(c.file)}"
    return (
        f"{_n(r.entries, 'entry', 'entries')} in {quote(c.file)}, {r.matched} still found, "
        f"{len(r.stale)} stale"
    )


def to_agent_check(checked: list[Checked]) -> str:
    lines = [f"{_check_line(c)}, {_n(c.result.new, 'new finding')} not in it" for c in checked]
    out = [f"agentprint baseline --check: {lines[0]}"]
    out += [f"personal: {line}" for line in lines[1:]]
    stale = [e for c in checked for e in c.result.stale]
    if stale:
        out.append("stale (the finding is gone, so the entry can be dropped):")
        out += [f"  - {e.check} {quote(e.path)} {quote(e.reason)}" for e in stale]
    last = ["Nothing was changed."]
    if stale:
        last.append("To drop the stale entries: agentprint baseline --prune.")
    if any(c.result.new for c in checked):
        last.append("To see the new findings: agentprint scan.")
    out.append(" ".join(last))
    return "\n".join(out)


def _write_data(w: Written) -> dict:
    c = w.change
    return {
        "baseline": clean(w.file),
        "entries": len(c.entries),
        "added": c.added,
        "kept": c.kept,
        "dropped": c.dropped,
    }


def to_json_write(written: list[Written], mode: str) -> str:
    data: dict[str, object] = {"tool": "agentprint", "version": __version__, "mode": mode}
    data |= _write_data(written[0])
    if len(written) > 1:
        data["personal"] = _write_data(written[1])
    data["ask_human"] = asks(written)
    return json.dumps(data, indent=2, ensure_ascii=False)


def _check_data(c: Checked) -> dict:
    r = c.result
    stale = [{"check_id": e.check, "path": e.path, "reason": e.reason} for e in r.stale]
    data = {"baseline": clean(c.file), "entries": r.entries, "matched": r.matched}
    return data | {"stale": stale, "new": r.new}


def to_json_check(checked: list[Checked]) -> str:
    data: dict[str, object] = {"tool": "agentprint", "version": __version__, "mode": "check"}
    data |= _check_data(checked[0])
    if len(checked) > 1:
        data["personal"] = _check_data(checked[1])
    return json.dumps(data, indent=2, ensure_ascii=False)
