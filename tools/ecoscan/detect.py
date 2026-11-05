"""Which file paths look like AI-agent files, and the (redacted) keys inside them.

Two layers:
- `ap006`: the path matches one of agentprint's own package rules (ALL_AGENTS). This is what
  `agentprint pack` reports today.
- candidate: a broader net over the folders and files of agents agentprint does not cover yet,
  so results can be re-checked when new agent modules land. Names only; nothing is judged here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentprint.agents import ALL_AGENTS
from agentprint.pack import PackageListing
from agentprint.secrets import find_secrets, redact

MAX_CANDIDATES_KEPT = 1000  # per package; the full count is kept separately

_B = r"(?:^|/)"  # path start or a folder boundary
CANDIDATE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (agent, re.compile(pattern))
    for agent, pattern in (
        ("claude-code", _B + r"(?:\.claude/|\.claude\.json$|CLAUDE(?:\.local)?\.md$)"),
        ("mcp", _B + r"\.mcp\.json$"),
        ("codex", _B + r"\.codex/"),
        ("agents-md", _B + r"AGENTS\.md$"),
        ("cursor", _B + r"(?:\.cursor/|\.cursorrules$)"),
        ("gemini", _B + r"(?:\.gemini/|GEMINI\.md$)"),
        ("windsurf", _B + r"(?:\.windsurf/|\.codeium/|\.windsurfrules$)"),
        ("opencode", _B + r"(?:\.opencode/|opencode\.jsonc?$)"),
        ("aider", _B + r"\.aider[^/]*(?:/|$)"),
        ("continue", _B + r"\.continue/"),
        ("cline", _B + r"(?:\.clinerules(?:/|$)|\.cline/)"),
        ("roo", _B + r"(?:\.roo/|\.roomodes$)"),
        ("kiro", _B + r"\.kiro/"),
        ("amazon-q", _B + r"\.amazonq/"),
        ("copilot", _B + r"\.github/copilot-instructions\.md$"),
        ("junie", _B + r"\.junie/"),
        ("goose", _B + r"\.goosehints$"),
    )
)

_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "history",
        re.compile(
            r"\.jsonl$|history|"
            + _B
            + r"(?:sessions|archived_sessions|paste-cache|file-history|transcripts|attachments)/"
        ),
    ),
    (
        "settings",
        re.compile(
            r"(?:^|/|\.)(?:settings|config|conf|mcp|mcp_config|auth|claude|opencode)[^/]*"
            r"\.(?:json|jsonc|toml|ya?ml)$|(?:^|/)\.env"
        ),
    ),
    ("instructions", re.compile(r"\.(?:md|mdc|txt)$|rules$|(?:^|/)\.(?:roomodes|goosehints)$")),
)


@dataclass(frozen=True)
class Candidate:
    path: str
    agent: str
    kind: str  # history | settings | instructions | other
    ap006: bool  # also matches an agentprint package rule

    def to_dict(self) -> dict:
        return {"path": self.path, "agent": self.agent, "kind": self.kind, "ap006": self.ap006}


def _ap006(path: str) -> bool:
    return any(rule.pattern.search(path) for a in ALL_AGENTS for rule in a.package_rules)


def candidate(path: str) -> Candidate | None:
    for agent, pattern in CANDIDATE_RULES:
        if pattern.search(path):
            kind = next((k for k, p in _KINDS if p.search(path)), "other")
            return Candidate(path, agent, kind, _ap006(path))
    if _ap006(path):  # an agentprint rule this list does not know yet
        return Candidate(path, "agentprint-rule", "other", True)
    return None


def is_candidate(path: str) -> bool:
    return candidate(path) is not None


def secret_findings(data: bytes) -> list[dict]:
    """Key-shaped strings in one file: rule id, first line, redacted value, count. Never the key."""
    if b"\x00" in data[:8192]:
        return []  # binary
    text = data.decode("utf-8", errors="replace")
    found: dict[str, dict] = {}
    for match in find_secrets(text):
        if match.value in found:
            found[match.value]["count"] += 1
            continue
        line = text.count("\n", 0, match.start) + 1
        found[match.value] = {
            "line": line,
            "rule": match.rule_id,
            "redacted": redact(match.value),
            "count": 1,
        }
    return list(found.values())


def new_record(eco: str, name: str, version: str | None = None) -> dict:
    """One JSONL line per package. Holds names, paths, counts and redacted values only."""
    return {
        "eco": eco,
        "name": name,
        "version": version,
        "status": "ok",  # ok | gone | skipped | error
        "reason": None,
        "method": None,
        "files": 0,
        "candidate_count": 0,
        "candidates": [],
        "secrets": [],
        "not_scanned": [],  # candidate content not read: link, too large, or archive budget
        "too_large": [],  # archives not downloaded at all
    }


def _relative(name: str, strip_root: bool) -> str:
    # npm tarballs wrap everything in "package/", sdists in "<name>-<version>/".
    return name.split("/", 1)[1] if strip_root and "/" in name else name


def examine(rec: dict, listing: PackageListing, archive: str, *, strip_root: bool) -> None:
    """Add one archive's candidate paths and redacted key findings to a package record."""
    rec["files"] += len(listing.files)
    for name in listing.files:
        found = candidate(name)
        if found is None:
            continue
        path = _relative(name, strip_root)
        rec["candidate_count"] += 1
        if len(rec["candidates"]) < MAX_CANDIDATES_KEPT:
            rec["candidates"].append({"archive": archive, **found.to_dict(), "path": path})
        data = listing.contents.get(name)
        if data is not None:
            for hit in secret_findings(data):
                where = {"archive": archive, "path": path, "agent": found.agent}
                rec["secrets"].append({**where, "kind": found.kind, "ap006": found.ap006, **hit})
    rec["not_scanned"] += [_relative(n, strip_root) for n in listing.skipped]
