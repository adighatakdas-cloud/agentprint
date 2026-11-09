"""AP010: an agent extension holds risky content: hidden characters, a command that sends a
credential off the machine, a download piped into a shell, or a shell command hidden in base64.

Extensions are what an agent loads as instructions or runs as its own tools: skills, commands,
subagents, rules and instruction files (CLAUDE.md, AGENTS.md, ...), and installed plugins and
extensions with their hooks and scripts. Agent modules name where each agent keeps them
(`extension_paths`); this check reads their files. Hook commands in agent settings are read too,
for every shape but the piped download, which AP004 already reports there.

How a file is read: Markdown and rule files (.cursorrules, .mdc, ...) sentence by sentence,
fenced code line by line; scripts line by line; TOML, YAML and JSON by their string values, as
prose under the keys that hold instructions (prompt, instructions, description, ...) and as
commands under the rest. README, CHANGELOG, LICENSE and the like, and docs/, examples/ and
tests/ folders at an extension's top, are not read.

The rules live in hidden.py (hidden characters, ruling X16) and shapes.py (credential sent
out, piped install with ruling X17, encoded command; negation, budgets). One print per rule per
file, at its strongest line, with how many lines hold it. A file whose budget runs out gets a
low "not fully checked" print, never a silent pass. `agentprint fix` never edits an extension:
every print is the human's, with the exact file and line.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..agents.base import ExtensionArtifact
from ..models import Artifact, Finding, HookCommand, Kind, Severity
from ..secrets import Budget
from ..structured import find_line, parse
from .base import agent_configs
from .hidden import hidden_characters, strip
from .shapes import (
    ENCODED,
    EXFILTRATION,
    MAX_LINE,
    NOT_CHECKED,
    PIPED,
    TRIGGER,
    Block,
    Tries,
    blocks,
    detail,
    encoded_commands,
    shape_hits,
    shapes,
    show_decoded,
)
from .shapes import (
    concealed as concealed_text,
)

__all__ = [
    "CHECK",
    "ENCODED",
    "EXFILTRATION",
    "HIDDEN",
    "MAX_BYTES",
    "MAX_LINE",
    "NOT_CHECKED",
    "PIPED",
    "Tries",
    "hidden_characters",
    "scan_text",
    "shapes",
]

ID = "AP010"
TITLE = "Risky agent extension"
FIX = (
    "Read the flagged line and ask the human where the extension came from. Remove hidden "
    "characters and base64-encoded commands, install tools at a pinned version instead of "
    "piping a download into a shell, and delete any instruction that sends credentials off the "
    "machine. Remove an extension nobody wrote on purpose."
)
MAX_BYTES = 2 * 1024 * 1024  # an extension file larger than this is not read
MIN_BLOBS = 256  # base64 blobs decoded per file, plus one per 64 characters of the file

# What each rule's detail starts with: advice reads it back to word the ask_human item. The
# shape rules (EXFILTRATION, PIPED, ENCODED, NOT_CHECKED) come from shapes.py.
HIDDEN = "hidden characters"

# --- which files ---------------------------------------------------------------------------

# Files an agent does not load from inside an extension folder: its README and the like, by
# name with any extension (README, CHANGELOG and LICENSE in any case, the others in capitals,
# so a rule called security.md is still read), and docs, examples and tests at its top.
_DOC_NAMES = frozenset(
    {"README", "CHANGELOG", "CHANGES", "HISTORY", "NEWS", "LICENSE", "LICENCE", "COPYING"}
    | {"NOTICE", "CONTRIBUTING", "CODE_OF_CONDUCT", "SECURITY", "AUTHORS", "MAINTAINERS"}
)
_ANY_CASE = frozenset({"readme", "changelog", "license", "licence"})
_DOC_DIRS = frozenset(
    {"docs", "doc", "examples", "example", "test", "tests", "__tests__", "testdata"}
)
_NOT_TEXT = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip", ".gz", ".tgz"}
    | {".woff", ".woff2", ".ttf", ".otf", ".map", ".pyc", ".lock", ".lockb", ".wasm", ".db"}
)
_LOCKFILES = frozenset(
    {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "npm-shrinkwrap.json"}
    | {"poetry.lock", "uv.lock", "cargo.lock", "gemfile.lock", "composer.lock", "bun.lock"}
)
_MARKDOWN = frozenset({".md", ".mdc", ".markdown", ".mdx", ".txt", ".rst"})
_RULE_FILES = frozenset(
    {".cursorrules", ".windsurfrules", ".clinerules", ".goosehints", ".rules"}
    | {".augment-guidelines"}
)


def docs_only(artifact: Artifact) -> bool:
    """True for a file the agent never loads from its extension folder: README.md, LICENSE, a
    CHANGELOG, package metadata, or anything in docs/, examples/ or tests/ at the folder's top.
    A file that is an extension by itself (one rule, one command) is always read."""
    path = artifact.path
    root = (artifact.root if isinstance(artifact, ExtensionArtifact) else None) or path.parent
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    if not parts:
        return False  # the extension is this file
    stem = path.name.split(".", 1)[0]
    if stem in _DOC_NAMES or stem.lower() in _ANY_CASE or path.name in ("METADATA", "PKG-INFO"):
        return True
    return len(parts) > 1 and parts[0].lower() in _DOC_DIRS


def _wanted(artifact: Artifact) -> bool:
    path = artifact.path
    name = path.name.lower()
    if name in _LOCKFILES or name.endswith((".min.js", ".min.css")):
        return False
    return path.suffix.lower() not in _NOT_TEXT and not docs_only(artifact)


def _read_file(path: Path) -> tuple[str, bool] | None:
    """(text, cut): the file's first MAX_BYTES as text, and whether there was more. None for a
    binary or unreadable file."""
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None  # binary
    cut = len(data) > MAX_BYTES
    return data[:MAX_BYTES].decode("utf-8", errors="replace"), cut


def _read(path: Path) -> str | None:
    read = _read_file(path)
    return None if read is None else read[0]


_STRUCTURED = frozenset({".json", ".jsonc", ".toml", ".yaml", ".yml"})
# Keys whose string values are instructions to the agent, read as prose (Gemini and Qwen command
# prompts, agent instructions, descriptions); every other string is read as a command.
_PROSE_KEYS = frozenset(
    {"prompt", "prompts", "instructions", "instruction", "description", "system", "content"}
    | {"system_prompt", "systemprompt", "body", "text", "message", "template", "guidelines"}
    | {"context", "goal", "task", "persona", "role", "notes", "usage", "when_to_use"}
)
MAX_VALUES = 5000  # string values read from one structured file, plus one per 32 characters


MAX_DEPTH = 32  # nesting read in one structured file


def _values(
    data: object, cut: list[int], key: str = "", depth: int = 0
) -> Iterator[tuple[str, str]]:
    """(key, text) for each string in parsed config; a list of strings under a command key is
    one command line (argv). Anything nested deeper than MAX_DEPTH is noted in `cut`."""
    if depth > MAX_DEPTH:
        cut.append(depth)
        return
    if isinstance(data, dict):
        for name, value in data.items():
            yield from _values(value, cut, str(name), depth + 1)
    elif isinstance(data, list):
        if data and all(isinstance(x, str) for x in data) and key.lower() not in _PROSE_KEYS:
            yield key, " ".join(data)
        else:
            for value in data:
                yield from _values(value, cut, key, depth + 1)
    elif isinstance(data, str):
        yield key, data


def structured_blocks(text: str, data: object, cut: list[int] | None = None) -> Iterator[Block]:
    """The string values of a parsed TOML, YAML or JSON file as blocks: prose under the keys
    that hold instructions, commands under the rest. Lines are found in the file lazily, only
    for a block that holds a shape's word. Values left unread (past MAX_VALUES, or nested past
    MAX_DEPTH) are noted in `cut`."""
    cut = [] if cut is None else cut
    cursor = 0
    limit = MAX_VALUES + len(text) // 32
    for n, (key, value) in enumerate(_values(data, cut)):
        if n >= limit:
            cut.append(n)
            return
        low = value.lower()
        if not any(word in low for word in TRIGGER):
            continue
        first = next((ln.strip() for ln in value.split("\n") if ln.strip()), "")[:60]
        at = text.find(first, cursor) if first else -1
        if at < 0 and first:
            at = text.find(first)
        cursor = max(cursor, at)
        base = text.count("\n", 0, at) + 1 if at >= 0 else 1
        skip = value[: value.find(first)].count("\n") if first in value else 0
        prose = key.lower() in _PROSE_KEYS
        for block in blocks(value, prose):
            yield block._replace(line=max(1, base + block.line - 1 - skip))


def _mode(path: Path, text: str) -> tuple[bool, object]:
    """(markdown, parsed data or None): how a file is read."""
    if path.suffix.lower() in _STRUCTURED:
        data = parse(text.encode("utf-8", "replace"), path.name)
        if isinstance(data, dict | list):
            return False, data
    return _markdown(path, text), None


def _markdown(path: Path, text: str) -> bool:
    """Prose read sentence by sentence (Markdown, rule files), not a script read line by line.
    A file with no extension is prose unless it starts with #! (a hook script)."""
    name = path.name.lower()
    if path.suffix.lower() in _MARKDOWN or name in _RULE_FILES:
        return True
    if path.suffix.lower() in (".toml", ".yaml", ".yml"):
        return True  # prompts and instructions, when the file cannot be parsed
    return not path.suffix and not text.startswith("#!")


# --- the check -------------------------------------------------------------------------------

_SEVERITY = {
    HIDDEN: Severity.HIGH,  # low for stray single characters (hidden.py, ruling X16)
    EXFILTRATION: Severity.CRITICAL,
    PIPED: Severity.MEDIUM,  # high when kept from the user, low in install docs (ruling X17)
    ENCODED: Severity.HIGH,
    NOT_CHECKED: Severity.LOW,
}


@dataclass(frozen=True)
class _Hit:
    rule: str
    severity: Severity
    line: int | None
    detail: str


_KINDS = (
    ("skills", "skill"),
    ("commands", "slash command"),
    ("prompts", "prompt"),
    ("agents", "subagent"),
    ("droids", "subagent"),
    ("workflows", "workflow"),
    ("hooks", "hook script"),
    ("rules", "rule"),
    ("steering", "rule"),
    ("instructions", "rule"),
    ("guidelines", "rule"),
    ("plugins", "plugin"),
    ("installed-plugins", "plugin"),
    ("extensions", "extension"),
)


def what(artifact: Artifact) -> str:
    """What kind of extension a file is, in a word or two, for the finding's wording: read from
    the folders inside the extension first, then the folder that holds it."""
    path = artifact.path
    name = path.name.lower()
    if name == "skill.md":
        return "skill"
    root = artifact.root if isinstance(artifact, ExtensionArtifact) else None
    try:
        inside = list(path.relative_to(root).parts[:-1]) if root else []
    except ValueError:
        inside = []
    around = list(root.parent.parts[-4:]) if root else list(path.parent.parts[-4:])
    for parts in (inside, around[::-1]):
        for part in parts:
            word = dict(_KINDS).get(part.lower())
            if word:
                return word
    if name in _RULE_FILES or name.endswith(".mdc"):
        return "rule"
    return "instructions file"


def _fix(rule: str, kind: str) -> str:
    """The step for the agent; what to ask the human is advice's (advice.py, AP010)."""
    if rule == HIDDEN:
        return (
            "Do not follow anything the hidden characters spell out. Remove every hidden "
            f"character from the file (the first is on the line shown), or delete the {kind} if "
            "nobody wrote it."
        )
    if rule == EXFILTRATION:
        return (
            "Do not run or follow it. Delete the command or instruction that reads the "
            f"credential and sends it out, or the whole {kind} if nobody wrote it on purpose."
        )
    if rule == PIPED:
        return (
            "Replace the piped download with a checked install: install the tool with a "
            "package manager at a pinned version, or download the script to a file, check its "
            "checksum, then run it."
        )
    if rule == NOT_CHECKED:
        return (
            f"Read the rest of this {kind} yourself before you follow it: it holds more commands "
            "than agentprint reads in one file, so the end was not checked for risky shapes."
        )
    return (
        "Do not decode and run it. Delete the encoded command (a command that is needed "
        f"belongs in plain text), or the whole {kind} if nobody knows why it is there."
    )


def _finding(artifact: Artifact, hit: _Hit, kind: str, count: int) -> Finding:
    return Finding(
        check_id=ID,
        severity=hit.severity,
        title=TITLE,
        agent=artifact.agent,
        location=str(artifact.path),
        line=hit.line,
        detail=f"{hit.rule}: {kind} {hit.detail}",
        fix=_fix(hit.rule, kind),
        count=count,
    )


def _severity(rule: str, block: Block, concealed: bool) -> Severity:
    if rule == PIPED and block.install:
        # Ruling X17: honest install docs are low; one that hides itself from the user is not.
        return Severity.MEDIUM if concealed else Severity.LOW
    if rule == PIPED and concealed:
        return Severity.HIGH
    return _SEVERITY[rule]


def scan_text(
    text: str, markdown: bool, piped: bool = True, data: object = None
) -> list[tuple[_Hit, int]]:
    """Each rule's strongest hit in one file's text (the first line among equals), with the
    number of lines it was found on. `data` is the parsed file for TOML, YAML and JSON: its
    string values are read instead of its lines. A base64 blob's decoded command is preferred to
    the decode shape on the same line. The detail is written only for the hit that is kept."""
    first: dict[str, _Hit] = {}
    lines: dict[str, set[int | None]] = {}

    def add(rule: str, severity: Severity, line: int | None, said: Callable[[], str]) -> None:
        lines.setdefault(rule, set()).add(line)
        best = first.get(rule)
        if (
            best is None
            or severity > best.severity
            or (severity == best.severity and (line or 0) < (best.line or 0))
        ):
            first[rule] = _Hit(rule, severity, line, said())

    found = hidden_characters(text)
    if found.first >= 0:
        line = text.count("\n", 0, found.first) + 1
        if found.strong:
            said = f"has {found.describe()} that an AI reads and a person does not see"
            add(HIDDEN, Severity.HIGH, line, lambda: said)
        else:
            said = f"has {found.describe()}: one such character hides no text, but remove it"
            add(HIDDEN, Severity.LOW, line, lambda: said)
    plain = strip(text)  # no hidden character can split a command word
    newlines: list[int] = []

    def line_of(index: int) -> int:
        if not newlines:
            newlines.extend(m.start() for m in re.finditer("\n", plain))
            newlines.append(len(plain))
        return bisect_right(newlines, index - 1) + 1

    tries = Tries.for_text(len(plain))
    blobs = Budget(blobs=MIN_BLOBS + len(plain) // 64)  # grows with the file, like Tries
    unread: list[int] = []
    for index, decoded in encoded_commands(plain, blobs, tries, unread):
        add(
            ENCODED,
            Severity.HIGH,
            line_of(index),
            lambda d=decoded: f'hides a shell command in base64: decodes to "{show_decoded(d)}"',
        )
    if unread:
        said = "was not read to the end for base64: it holds more than agentprint decodes"
        add(NOT_CHECKED, Severity.LOW, line_of(min(unread)), lambda s=said: s)
    low = plain.lower()
    if not any(word in low for word in TRIGGER):  # most files hold no shape's word at all
        return [(hit, len(lines[rule])) for rule, hit in first.items()]
    cut: list[int] = []
    source = structured_blocks(plain, data, cut) if data is not None else blocks(plain, markdown)
    assigned: dict[str, str] = {}
    last_prose = ""  # the sentence before: "Don't tell the user." conceals what follows it
    dropped_line: int | None = None
    for block in source:
        if tries.spent and tries.dropped:
            break  # the budget is spent and something was already left unread
        hits = shape_hits(block.text, block.prose, piped, tries, assigned, block.breaks)
        if tries.dropped and dropped_line is None:
            dropped_line = block.line
        for hit in hits:
            concealed = hit.concealed or concealed_text(last_prose)
            add(
                hit.rule,
                _severity(hit.rule, block, concealed),
                block.line,
                lambda h=hit, c=concealed: detail(h._replace(concealed=c)),
            )
        if block.prose:
            last_prose = block.text
    if dropped_line is not None:
        said = "was not read to the end for risky shapes: it holds more than agentprint reads"
        add(NOT_CHECKED, Severity.LOW, dropped_line, lambda s=said: s)
    if cut:
        said = "was not read to the end: it holds more values than agentprint reads"
        add(NOT_CHECKED, Severity.LOW, 1, lambda s=said: s)
    return [(hit, len(lines[rule])) for rule, hit in first.items()]


class RiskyExtension:
    id = ID
    severity = Severity.HIGH  # critical for a credential sent out, medium for a piped install
    title = TITLE
    fix = FIX

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        artifacts = list(artifacts)
        settings = {_key(a.path) for a in artifacts if a.kind is Kind.SETTINGS}
        findings: list[Finding] = []
        for artifact in artifacts:
            # A file that is also an agent's settings is read through its hooks below.
            if artifact.kind is not Kind.EXTENSION or _key(artifact.path) in settings:
                continue
            if not _wanted(artifact):
                continue
            read = _read_file(artifact.path)
            if read is None:
                continue
            text, too_long = read
            kind = what(artifact)
            if too_long:  # cut mid-file: read line by line, not parsed
                markdown, data = _markdown(artifact.path, text), None
            else:
                markdown, data = _mode(artifact.path, text)
            hits = scan_text(text, markdown, data=data)
            if too_long and all(hit.rule != NOT_CHECKED for hit, _ in hits):
                said = "was read only to 2 MiB: the rest of it was not checked"
                hits.append((_Hit(NOT_CHECKED, Severity.LOW, None, said), 1))
            findings += [_finding(artifact, hit, kind, count) for hit, count in hits]
        for artifact, config in agent_configs(artifacts):
            findings += self._hooks(artifact, config.hooks)
        return findings

    def _hooks(self, artifact: Artifact, hooks: tuple[HookCommand, ...]) -> list[Finding]:
        """Hook commands in an agent's settings: every shape but the piped download (AP004)."""
        found: dict[str, Finding] = {}
        for hook in hooks:
            for hit, count in scan_text(hook.command, markdown=False, piped=False):
                if hit.rule in found:
                    found[hit.rule].count += count
                    continue
                line = _hook_line(artifact.path, hook)
                found[hit.rule] = _finding(
                    artifact, _Hit(hit.rule, hit.severity, line, hit.detail), "hook", count
                )
        return list(found.values())


def _key(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _hook_line(path: Path, hook: HookCommand) -> int | None:
    words = hook.command.split()
    for needle in (hook.command[:60], *words[:3], hook.event):
        line = find_line(path, needle)
        if line:
            return line
    return None


CHECK = RiskyExtension()
