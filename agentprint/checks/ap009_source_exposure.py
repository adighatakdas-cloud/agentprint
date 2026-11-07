"""AP009: a package hands an attacker's AI your original source, or a map of it.

Nobody can stop the reverse engineering of code you ship. What this check shows, before you
publish, is what in a package makes it easy: source maps (with or without the original source
inside them), original TypeScript/Vue/Svelte source next to the compiled code, and AI
instruction files that describe the codebase.

Shipping source is normal for open source, so every item is judged by the package's licence
and an open-source package is never nagged:

| Exposure                                             | Closed source | Open source |
|--------------------------------------------------|---------------|-------------|
| source maps holding `sourcesContent`             | high          | low         |
| source maps without it (layout, original names)  | medium        | -           |
| original source next to the compiled code        | medium        | -           |
| AI instruction files                             | medium        | -           |

One finding per kind per package: a count and one example path, never file contents.
Content is only what `pack` read within its limits. A file that was not read is never guessed
at: an unread `.map` still counts by its name (it ships, whatever is inside), an unread bundle
is not checked for an inline map, and the package's first AP009 finding says how many files
were not read. Bundled dependencies (`node_modules/`) are not your source and are skipped.
Runs in `pack` and the publish guard only: pushing source to your own repository is normal.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

from ..models import Finding, Severity
from ..pack import PackageListing

ID = "AP009"
TITLE = "Package lets an AI rebuild your source"
FIX = (
    "Turn off source maps or their sourcesContent in the build, and keep original source and "
    'AI instruction files out of the package (.npmignore or the package.json "files" list; '
    "your Python build tool's exclude list)."
)
_KEEP_OUT = '.npmignore or the package.json "files" list; your Python build tool\'s exclude list'
MAP_FIX = (
    'Turn off source maps, or the source inside them, in the build (tsc "sourceMap": false or '
    '"inlineSources": false; esbuild --sources-content=false; Vite build.sourcemap: false; '
    f"webpack devtool: false), or keep the .map files out of the package ({_KEEP_OUT})."
)
SOURCE_FIX = f"Keep original source (src/, .ts, .vue, .svelte) out of the package ({_KEEP_OUT})."
INSTRUCTIONS_FIX = f"Keep AI instruction files out of the package ({_KEEP_OUT})."

OPEN = "open-source package"
_ADD_ONE = "so treated as closed source; add one if it's open source"
_ADD_LICENCE = "so treated as closed source; add a licence if it's open source"
NPM_MISSING = f"no licence field, {_ADD_ONE}"
NPM_UNREADABLE = f"package.json could not be read, {_ADD_LICENCE}"
PYPI_MISSING = f"no licence metadata, {_ADD_ONE}"
PYPI_UNREADABLE = f"package metadata could not be read, {_ADD_LICENCE}"
NOTHING_FOUND = f"no package.json or package metadata found, {_ADD_LICENCE}"
CUSTOM_ID = (
    "uses a custom licence id, so treated as closed source; use an SPDX licence id if it's "
    "open source"
)
PROPRIETARY = "License :: Other/Proprietary License"
# Licence values that say "closed source" outright (compared trimmed and case-insensitively).
NPM_CLOSED_WORDS = ("proprietary", "commercial")
PYPI_CLOSED_WORDS = ("proprietary", "commercial", "all rights reserved")
_LICENSE_REF = re.compile(r"\bLicenseRef-[A-Za-z0-9.-]+", re.IGNORECASE)  # a custom SPDX id

_BUNDLE = (".js", ".mjs", ".cjs")
_ORIGINAL = re.compile(r"\.(tsx?|mts|cts|vue|svelte)$")
_DECLARATION = re.compile(r"\.d(\.[^./]+)?\.(ts|mts|cts)$")  # .d.ts, .d.mts, .d.css.ts
_INSTRUCTIONS = re.compile(
    r"(^|/)(CLAUDE\.md|AGENTS\.md|GEMINI\.md|\.cursorrules|\.windsurfrules)$"
    r"|(^|/)\.github/copilot-instructions\.md$"
    r"|(^|/)\.cursor/rules/."
)
_NPM_TOP = re.compile(r"^(package/)?package\.json$")  # npm's fixed folder, or a project folder
_NPM_ANY = re.compile(r"^[^/]+/package\.json$")  # a tarball made with another top folder
_PYPI = re.compile(r"^([^/]+\.dist-info/METADATA|([^/]+/)?PKG-INFO)$")
_INLINE_AT = b"sourceMappingURL=data:"
# The comment that ends a bundle with an inline map; the base64 must run to the end of file.
_INLINE = re.compile(
    rb"sourceMappingURL=data:application/json(?:;[^;,\s]*)*?;base64,([A-Za-z0-9+/]*=*)"
    rb"\s*(?:\*/\s*)?\Z"
)
_COMMENT_BEFORE = re.compile(rb"(//|/\*)[ \t]*[#@][ \t]*\Z")


def _ours(name: str) -> bool:
    return "node_modules" not in name.split("/")


def _is_map(name: str) -> bool:
    return name.lower().endswith(".map")


def _is_bundle(name: str) -> bool:
    return name.lower().endswith(_BUNDLE)


def _is_licence_file(name: str) -> bool:
    return bool(_NPM_TOP.match(name) or _NPM_ANY.match(name) or _PYPI.match(name))


def wants(name: str) -> bool:
    """The members whose contents AP009 reads: source maps, bundles (for an inline map at the
    end) and the files that hold the package's licence."""
    if _is_licence_file(name):
        return True
    return _ours(name) and (_is_map(name) or _is_bundle(name))


# ---- the licence ---------------------------------------------------------------------------


def _licence_file(files: list[str]) -> tuple[str, str] | None:
    """('npm' | 'pypi', member) for the file that states the package's licence."""
    names = set(files)
    for name in ("package/package.json", "package.json"):
        if name in names:
            return "npm", name
    for name in files:
        if _PYPI.match(name):
            return "pypi", name
    for name in files:
        if _NPM_ANY.match(name):
            return "npm", name
    return None


def _npm_licence(raw: bytes | None) -> tuple[bool, str]:
    try:
        data = json.loads(raw) if raw is not None else None
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        return True, NPM_UNREADABLE
    value = data.get("license")
    if isinstance(value, dict):  # the old {"type": "MIT", "url": ...} form
        value = value.get("type")
    if value is None and isinstance(data.get("licenses"), list):  # the old array form
        types = [item.get("type") for item in data["licenses"] if isinstance(item, dict)]
        value = next((t for t in types if isinstance(t, str) and t.strip()), None)
    if not isinstance(value, str) or not value.strip():
        return True, NPM_MISSING
    text = " ".join(value.split()).lower()
    if text == "unlicensed":
        return True, "closed-source package: licence UNLICENSED"
    if text.startswith("see license in"):
        return True, "closed-source package: licence SEE LICENSE IN a file"
    if text in NPM_CLOSED_WORDS:
        return True, f"closed-source package: licence {text}"
    if _LICENSE_REF.search(text):
        return True, CUSTOM_ID
    return False, OPEN


def _headers(text: str) -> list[tuple[str, str]]:
    """(lower-case name, value) pairs of a METADATA / PKG-INFO header block."""
    headers: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            break  # the long description starts after the first blank line
        if line[0] in " \t" and headers:  # a continuation line
            key, value = headers[-1]
            headers[-1] = (key, f"{value} {line.strip()}")
            continue
        key, sep, value = line.partition(":")
        if sep:
            headers.append((key.strip().lower(), value.strip()))
    return headers


def _pypi_licence(raw: bytes | None) -> tuple[bool, str]:
    if raw is None:
        return True, PYPI_UNREADABLE
    headers = _headers(raw.decode("utf-8", errors="replace"))
    classifiers = [" ".join(value.split()) for key, value in headers if key == "classifier"]
    if PROPRIETARY in classifiers:
        return True, f"closed-source package: classifier {PROPRIETARY}"
    for key, value in headers:
        if key == "license-expression" and _LICENSE_REF.search(value):
            return True, CUSTOM_ID
        word = " ".join(value.split()).lower().rstrip(".")
        if key == "license" and word in PYPI_CLOSED_WORDS:
            return True, f"closed-source package: licence {word}"
    stated = any(
        key in ("license", "license-expression") and value and value.upper() != "UNKNOWN"
        for key, value in headers
    )
    osi = any(c.startswith("License :: OSI Approved") for c in classifiers)
    return (False, OPEN) if stated or osi else (True, PYPI_MISSING)


def licence_of(listing: PackageListing) -> tuple[bool, str]:
    """(closed source?, why), from package.json (npm) or METADATA / PKG-INFO (PyPI).

    Closed: npm `license` missing, `UNLICENSED`, `SEE LICENSE IN ...`, `proprietary`,
    `commercial`, or an expression with a custom `LicenseRef-...` id; PyPI the Other/Proprietary
    classifier, a License-Expression with a `LicenseRef-...` id, a License field that is
    `Proprietary`, `Commercial` or `All rights reserved`, or no License / License-Expression and
    no OSI classifier. A licence file that is missing or could not be read counts as closed, and
    `why` says so. `why` never repeats what the file says."""
    found = _licence_file(listing.files)
    if found is None:
        return True, NOTHING_FOUND
    kind, name = found
    raw = listing.contents.get(name)
    return _npm_licence(raw) if kind == "npm" else _pypi_licence(raw)


# ---- source maps ---------------------------------------------------------------------------


def _source_map(raw: bytes) -> dict | None:
    """The parsed source map, or None when this is not one."""
    if raw.startswith(b")]}'"):  # the guard prefix the source map spec allows
        raw = raw[4:]
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):  # includes bad JSON and bad UTF-8
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("mappings"), str) or isinstance(data.get("sections"), list):
        return data
    return None


def _full_source(data: dict, nested: bool = False) -> bool:
    """True when the map holds original source: a sourcesContent list with at least one
    non-empty string (in itself, or in a section of an index map)."""
    content = data.get("sourcesContent")
    if isinstance(content, list) and any(isinstance(s, str) and s.strip() for s in content):
        return True
    sections = data.get("sections")
    if nested or not isinstance(sections, list):
        return False
    return any(
        isinstance(section, dict)
        and isinstance(section.get("map"), dict)
        and _full_source(section["map"], nested=True)
        for section in sections
    )


def _inline_map(raw: bytes) -> dict | None:
    """The inline `sourceMappingURL=data:application/json;base64,...` map ending a bundle."""
    at = raw.rfind(_INLINE_AT)
    if at < 0 or not _COMMENT_BEFORE.search(raw, max(0, at - 16), at):
        return None
    match = _INLINE.match(raw, at)
    if match is None:
        return None
    try:
        decoded = base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError):
        return None
    return _source_map(decoded)


# ---- the check -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Exposure:
    one: str  # what one file of this kind does to you, e.g. "source map holds your original source"
    many: str
    agent: str
    fix: str
    closed: Severity
    open: Severity | None  # None: never mentioned for an open-source package


FULL = Exposure(
    "source map holds your original source",
    "source maps hold your original source",
    "build",
    MAP_FIX,
    Severity.HIGH,
    Severity.LOW,
)
LAYOUT = Exposure(
    "source map gives away your file layout and original names",
    "source maps give away your file layout and original names",
    "build",
    MAP_FIX,
    Severity.MEDIUM,
    None,
)
ORIGINAL = Exposure(
    "original source file ships next to the compiled code",
    "original source files ship next to the compiled code",
    "build",
    SOURCE_FIX,
    Severity.MEDIUM,
    None,
)
INSTRUCTIONS = Exposure(
    "AI instruction file describes your codebase to an AI",
    "AI instruction files describe your codebase to an AI",
    "ai-instructions",
    INSTRUCTIONS_FIX,
    Severity.MEDIUM,
    None,
)
KINDS = (FULL, LAYOUT, ORIGINAL, INSTRUCTIONS)  # the order findings come in


def _sort(listing: PackageListing) -> tuple[dict[Exposure, list[str]], int]:
    """Each kind's files, in listing order, and how many maps and bundles were not read."""
    found: dict[Exposure, list[str]] = {kind: [] for kind in KINDS}
    not_read = 0
    compiled = False
    for name in filter(_ours, listing.files):
        raw = listing.contents.get(name)
        lower = name.lower()
        if _is_map(name):
            if raw is None:
                found[LAYOUT].append(name)  # it ships, whatever is inside; said to be unread
                not_read += 1
            elif (data := _source_map(raw)) is not None:
                found[FULL if _full_source(data) else LAYOUT].append(name)
        elif _is_bundle(name):
            compiled = True
            if raw is None:
                not_read += 1
            elif (data := _inline_map(raw)) is not None:
                found[FULL if _full_source(data) else LAYOUT].append(name)
        elif _ORIGINAL.search(lower) and not _DECLARATION.search(lower):
            found[ORIGINAL].append(name)
        if _INSTRUCTIONS.search(name):
            found[INSTRUCTIONS].append(name)
    if not compiled:
        found[ORIGINAL] = []  # source with no compiled code next to it is the package itself
    return found, not_read


def check_listing(listing: PackageListing) -> list[Finding]:
    found, not_read = _sort(listing)
    closed, why = licence_of(listing)
    findings: list[Finding] = []
    for kind, names in found.items():
        severity = kind.closed if closed else kind.open
        if not names or severity is None:
            continue
        what = kind.one if len(names) == 1 else kind.many
        findings.append(
            Finding(
                check_id=ID,
                severity=severity,
                title=TITLE,
                agent=kind.agent,
                location=f"{listing.label}!{names[0]}",
                line=None,
                detail=f"{len(names)} {what} ({why}), e.g. {names[0]}",
                fix=kind.fix,
            )
        )
    if findings and not_read:
        files = "1 file" if not_read == 1 else f"{not_read} files"
        note = f"; {files} not read (a link, too large, or package limit reached), so not checked"
        findings[0].detail += note
    return findings
