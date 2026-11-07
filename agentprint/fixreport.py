"""`agentprint fix` for AI agents and tools: the agent text and the JSON (a person gets
ui/fixview.py). Every view counts fixes with `rows()`, so "3 fixes ready" means the same thing
in each of them.

Line 1 of the agent text is the verdict after fixing. Every value read from the scanned files
(paths, MCP server names, ignore lines, masked stubs) goes through sanitize.quote(), so it reads
as data. Keys appear only as masked stubs (the secrets.redact form), never in full: a plan
never holds a key.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .advice import Moment
from .fix import Change, Outcome, Plan
from .models import Finding
from .report import DATA_NOTE, FailOn, exit_code, finding_dict, summary
from .sanitize import clean, quote

UNDOABLE = ("ignore", "lock")  # the kinds of change `agentprint fix --undo` can put back
RERUN = "re-run with --yes to apply"


@dataclass(frozen=True)
class Row:
    """One line of a fix list: one change, or every lock change for one AP008 print (an agent
    folder with 28 readable files is one row: "28 files")."""

    kind: str
    check_id: str
    path: Path  # the file, or the agent folder for a group of locks
    line: int | None
    changes: tuple[Change, ...]


def rows(changes: Iterable[Change], findings: Iterable[Finding] = ()) -> list[Row]:
    """The changes as list rows, in plan order. Locks of files that one AP008 print found
    together (its `paths`) share a row named after that print's folder."""
    folders = [f for f in findings if f.check_id == "AP008" and len(f.paths) > 1]
    groups: dict[object, list[Change]] = {}
    for n, change in enumerate(changes):
        owner = None
        if change.kind == "lock":
            owner = next((f.location for f in folders if change.path in f.paths), None)
        groups.setdefault(("lock", owner) if owner else n, []).append(change)
    out = []
    for key, group in groups.items():
        first = group[0]
        if len(group) > 1 and isinstance(key, tuple):
            out.append(Row("lock", first.check_id, Path(key[1]), None, tuple(group)))
        else:
            out.append(Row(first.kind, first.check_id, first.path, first.line, (first,)))
    return out


def skipped_rows(
    outcome: Outcome | None, findings: Iterable[Finding] = ()
) -> list[tuple[Row, str]]:
    """What was not fixed, as rows with their reason. Files of one AP008 print that failed for
    the same reason share a row, as they do in the plan."""
    if outcome is None:
        return []
    findings = list(findings)
    out = []
    for reason in dict.fromkeys(why for _, why in outcome.skipped):
        same = [c for c, why in outcome.skipped if why == reason]
        out += [(row, reason) for row in rows(same, findings)]
    return out


def fixes(n: int) -> str:
    return f"{n} fix" if n == 1 else f"{n} fixes"


def server_name(change: Change) -> str:
    """The MCP server an env change is in (scanned text: quote or clean it before showing)."""
    try:
        return str(json.loads(change.json_path)[-3])
    except (ValueError, IndexError, TypeError):
        return "?"


def _where(path: Path, line: int | None) -> str:
    return quote(str(path)) + (f" line {line}" if line else "")


def describe(row: Row) -> str:
    """What one row changes, in words for an AI agent."""
    first = row.changes[0]
    if row.kind == "mask":
        places = sum(len(c.offsets) for c in row.changes)
        more = f" ({places} places)" if places > 1 else ""
        return f"key {quote(first.shown)} masked in place{more}"
    if row.kind == "env":
        reference = "${" + first.var + "}"
        server = quote(server_name(first))
        return f"env.{first.var} of MCP server {server} set to {quote(reference)}"
    if row.kind == "ignore":
        return f"line {quote(first.line_text)} added"
    if row.kind == "lock" and len(row.changes) > 1:
        return f"{len(row.changes)} files made private to the owner (chmod go-rwx)"
    if row.kind == "lock" and first.old_mode is not None:
        new = first.old_mode & ~0o077
        return f"mode {first.old_mode:04o} -> {new:04o} (private to the owner)"
    return quote(first.shown)


def _item(row: Row) -> str:
    return f"  - {row.kind} {row.check_id} {_where(row.path, row.line)}: {describe(row)}"


def to_agent_fix(
    plan: Plan,
    outcome: Outcome | None,
    after: list[Finding],
    fail_on: FailOn,
    *,
    dry_run: bool = False,
) -> str:
    """Compact text for an AI agent. Line 1 is the verdict after fixing. `outcome` is None when
    nothing was applied (no --yes, a dry run, or nothing to apply); `after` is the re-check, or
    the scan itself when nothing changed."""
    planned = rows(plan.changes, plan.findings)
    done = rows(outcome.applied, plan.findings) if outcome else []
    skipped = skipped_rows(outcome, plan.findings)
    waiting = outcome is None and bool(planned)  # a plan that was not applied
    blocking = [f for f in after if exit_code([f], fail_on)]

    if waiting:
        counts = [f"{fixes(len(planned))} ready"]
    elif outcome is not None:
        counts = [f"{len(done)} fixed"] + ([f"{len(skipped)} not fixed"] if skipped else [])
    else:
        counts = ["nothing to fix automatically" if plan.manual else "nothing to fix"]
    if plan.manual:
        counts.append(f"{len(plan.manual)} for the human")
    head = ", ".join(counts)
    if waiting:
        head += "; dry run: nothing changed" if dry_run else f"; nothing changed: {RERUN}"
    out = [f"agentprint fix: {'BLOCK' if blocking else 'CLEAR'} — {head}", DATA_NOTE]

    if done:
        out += ["fixed:", *(_item(row) for row in done)]
    if skipped:
        out.append("not_fixed:")
        out += [
            f"  - {row.kind} {row.check_id} {_where(row.path, row.line)}: {clean(why)}"
            for row, why in skipped
        ]
    if waiting:
        out += ["would_fix:", *(_item(row) for row in planned)]
    if blocking and not waiting:
        out.append("still_blocking:")
        for f in blocking:
            where = _where(Path(f.location), f.line)
            out.append(f"  - {f.severity.name.lower()} {f.check_id} {f.title}: {where}")
    if plan.manual:
        out.append("ask_human:")
        out += [f"  - {m.check_id}: {clean(m.text, None)}" for m in plan.manual]

    if waiting:
        rerun = "with --yes" + (" and without --dry-run" if dry_run else "")
        start = "Dry run: nothing was changed." if dry_run else "Nothing was changed."
        out.append(f"{start} To apply these fixes, run the same agentprint fix command {rerun}.")
        return "\n".join(out)
    last = []
    if plan.manual:
        last.append("Tell the human each ask_human item: these are theirs to do.")
    if skipped:
        last.append("Run the same agentprint fix command again for what was not fixed.")
    if blocking:
        last.append("For what still blocks, re-run the agentprint check and do its fix steps.")
    if outcome and any(c.kind in UNDOABLE for c in outcome.applied):
        last.append("agentprint fix --undo puts back the ignore and lock changes.")
    if last:
        out.append(" ".join(last))
    return "\n".join(out)


def _change(change: Change, reason: str | None = None) -> dict:
    data = {
        "kind": change.kind,
        "check_id": change.check_id,
        "path": clean(str(change.path)),
        "line": change.line,
        "shown": clean(change.shown),
    }
    if reason is not None:
        data["reason"] = clean(reason)
    return data


def to_json_fix(
    plan: Plan,
    outcome: Outcome | None,
    after: list[Finding],
    fail_on: FailOn,
    *,
    pack: bool = False,
) -> str:
    """The plan, what was applied or skipped, what needs the human, and the re-check. One entry
    per change (a folder of 28 locked files is 28 entries)."""
    moment = Moment.publish if pack else Moment.scan
    data = {
        "tool": "agentprint",
        "version": __version__,
        "verdict": "BLOCK" if exit_code(after, fail_on) else "CLEAR",
        "planned": [_change(c) for c in plan.changes],
        "applied": [_change(c) for c in outcome.applied] if outcome else [],
        "skipped": [_change(c, why) for c, why in outcome.skipped] if outcome else [],
        "needs_human": [{"check_id": m.check_id, "text": clean(m.text, None)} for m in plan.manual],
        "after": {
            "summary": summary(after),
            "findings": [finding_dict(f, moment) for f in after],
        },
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def to_agent_undo(restored: list[str], cannot: list[str], nothing: bool) -> str:
    """`agentprint fix --undo` for an AI agent. Line 1 says what happened."""
    if nothing:
        return "\n".join(["agentprint fix --undo: nothing to undo", *cannot])
    head = f"{len(restored)} restored"
    if cannot:
        head += f", {len(cannot)} can't be undone"
    out = [f"agentprint fix --undo: {head}"]
    if restored:
        out += ["restored:", *(f"  - {clean(text, None)}" for text in restored)]
    if cannot:
        out += ["not_restored:", *(f"  - {clean(text, None)}" for text in cannot)]
    return "\n".join(out)


def to_json_undo(restored: list[str], cannot: list[str], nothing: bool) -> str:
    data: dict = {"tool": "agentprint", "version": __version__, "nothing_to_undo": nothing}
    if nothing:
        data |= {"restored": [], "not_restored": [], "note": clean(" ".join(cannot), None)}
    else:
        data |= {
            "restored": [clean(t, None) for t in restored],
            "not_restored": [clean(t, None) for t in cannot],
        }
    return json.dumps(data, indent=2, ensure_ascii=False)
