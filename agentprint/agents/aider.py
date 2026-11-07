"""Aider.

Not installed on the verification Mac. Paths from the docs and the official source, read
2026-11-05. Aider writes its transcripts where it runs (the git root, else the current folder):
.aider.chat.history.md, .aider.input.history and, when asked, .aider.llm.history. Config
.aider.conf.yml is read from the home folder, then the git root, then the current folder, and can
hold openai-api-key, anthropic-api-key or api-key: provider=<key>. Keys also come from .env in the
same places, and ~/.aider/oauth-keys.env keeps an OpenRouter key from the sign-in. Everything is
plain text; Aider uses no keychain, no hooks and no MCP.
Docs: https://aider.chat/docs/config/aider_conf.html, https://aider.chat/docs/config/dotenv.html,
github.com/Aider-AI/aider aider/args.py, aider/main.py, aider/onboarding.py.

oauth-keys.env and .env are key stores: never secret-scanned, only checked for who can read them
(and, in a project, whether git would commit them). A .env counts as Aider's only next to Aider's
own files, so a project that never used Aider gets no print for its .env.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from .base import AgentDoc, artifacts, from_home, matches_any

NAME = "aider"
_HISTORY = (".aider.chat.history.md", ".aider.input.history", ".aider.llm.history")
_CONFIG = ".aider.conf.yml"

# Every Aider history file is a transcript or typed-input log (fix masks keys there).
_RECORDS = (re.compile(r"(^|/)\.aider\.(chat\.history\.md|input\.history|llm\.history)$"),)

DOC = AgentDoc(
    label="Aider",
    reads=(
        "transcripts `.aider.chat.history.md`, `.aider.input.history`, `.aider.llm.history` (in "
        "a project or the home folder); `.aider.conf.yml` (home and project)"
    ),
    keys="`~/.aider/oauth-keys.env`; `.env` next to Aider's files (home and project)",
    fix="masks keys in transcripts; keys in `.aider.conf.yml`: needs you",
    checked="docs and source, 2026-11-05",
)


def _uses_aider(folder: Path) -> bool:
    """True if Aider has left its own files in this folder."""
    names = (*_HISTORY, _CONFIG)
    try:
        return any((folder / name).is_file() for name in names) or (folder / ".aider").is_dir()
    except OSError:
        return False


class Aider:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.aider\.(chat\.history\.md|input\.history|llm\.history)$"),
            Severity.HIGH,
            "Aider transcript or typed-input history (written to the repo root).",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.aider/oauth-keys\.env$"),
            Severity.HIGH,
            "Aider's saved OpenRouter key.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.aider\.conf\.yml$"),
            Severity.MEDIUM,
            "Aider config; may hold API keys.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.aider\.tags\.cache\.v\d+/"),
            Severity.LOW,
            "Aider repo-map cache.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        yield from artifacts(NAME, Kind.SETTINGS, home / _CONFIG)
        yield from artifacts(NAME, Kind.HISTORY, *(home / name for name in _HISTORY))
        yield from artifacts(NAME, Kind.CREDENTIALS, home / ".aider" / "oauth-keys.env")
        if _uses_aider(home):
            yield from artifacts(NAME, Kind.CREDENTIALS, home / ".env")

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        yield from artifacts(NAME, Kind.HISTORY, *(root / name for name in _HISTORY), personal=True)
        yield from artifacts(NAME, Kind.SETTINGS, root / _CONFIG, personal=True)
        if _uses_aider(root):
            yield from artifacts(NAME, Kind.CREDENTIALS, root / ".env", personal=True)

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        return AgentConfig()  # no permissions, hooks or MCP servers in Aider's config
