"""What to do about each finding, written for the AI agent that will do it.

`fix_steps` are actions an agent can perform itself (edit a file, add an ignore line, untrack a
file). `ask_human` is what only the human can do, such as rotating a key in a provider console.

Checks plug in here, not in the report code: register an adviser for a check id with
`@adviser("AP0xx")`. A check with no adviser falls back to its one-line `fix` hint.
Every value taken from scanned content goes through `sanitize.quote()`.

`advise(..., person=True)` words the same advice for a person reading `agentprint fix`'s plan:
without what is said only to an AI agent (NO_LEAK, "The agent cannot do this"), and with
rotating as the reader's own step. An adviser that takes no `person` argument is the same for
both.
"""

from __future__ import annotations

import inspect
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .agents import ALL_AGENTS
from .checks import ap009_source_exposure as ap009
from .checks.ap004_network_hook import ASK_FIRST as HOOK_ASK_FIRST
from .checks.ap005_mcp_inline_secret import _MARKER as MCP_MARKER
from .checks.ap005_mcp_inline_secret import ASK_FIRST as MCP_ASK_FIRST
from .models import Finding, Severity
from .sanitize import clean, quote, safe_step
from .secrets import HIDDEN_MARKER, RULES, hidden_how


class Moment(StrEnum):
    """What the user is about to do; it changes which fix applies."""

    scan = "scan"  # a general check of this machine or project
    publish = "publish"  # about to publish a package
    push = "push"  # about to git push


@dataclass(frozen=True)
class Advice:
    fix_steps: list[str] = field(default_factory=list)
    ask_human: list[str] = field(default_factory=list)


# (finding, moment) -> Advice; an adviser may also take `person` (a keyword, default False).
Adviser = Callable[..., Advice]
ADVISERS: dict[str, Adviser] = {}

# Where to revoke and replace each kind of key. Checked 2026-11-05 against:
# anthropic  https://platform.claude.com/docs/en/get-api-key ("API keys" page link)
# openai     platform.openai.com/api-keys is the dashboard's keys page (OpenAI's help centre
#            pages were not fetchable; matches the dashboard's own navigation)
# github     https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/
#            managing-your-personal-access-tokens (Settings > Developer settings > Tokens)
# aws        https://docs.aws.amazon.com/IAM/latest/UserGuide/id-credentials-access-keys-update.html
# stripe     https://docs.stripe.com/keys ("Rotate an API key" on the API keys page)
# slack      https://docs.slack.dev/authentication/best-practices-for-security (api.slack.com/apps)
# google     https://docs.cloud.google.com/docs/authentication/api-keys (Credentials page)
ROTATE_AT: dict[str, str] = {
    "anthropic": "https://platform.claude.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
    "github": "https://github.com/settings/tokens",
    "aws": "https://console.aws.amazon.com/iam/",
    "stripe": "https://dashboard.stripe.com/apikeys",
    "slack": "https://api.slack.com/apps",
    "google": "https://console.cloud.google.com/apis/credentials",
}
_RULE_BY_NAME = {rule.name: rule.id for rule in RULES}
_PACKAGE_SUFFIXES = (".tgz", ".tar.gz", ".whl", ".zip")
NO_LEAK = "Never print, echo, copy or reuse the key's value while doing this."
# How the person-worded AP004 step starts instead of the check's own (agent) sentence.
HOOK_PERSON_FIRST = "If you did not add this hook, delete it."


def adviser(check_id: str) -> Callable[[Adviser], Adviser]:
    def register(fn: Adviser) -> Adviser:
        ADVISERS[check_id] = fn
        return fn

    return register


def advise(finding: Finding, moment: Moment, *, person: bool = False) -> Advice:
    """Fix steps and ask_human items, each shown whole and unchanged or replaced by SAFE_STEP.
    A check's fix can embed shell-quoted scanned paths: shortening or cleaning it could break
    the quoting and run part of a hostile file name, so it is never edited.

    `person`: worded for a person who reads the steps and does them (agentprint fix's human
    view), not for an AI agent. The agent wording (the default) is what every report shows."""
    fn = ADVISERS.get(finding.check_id)
    if fn is None:
        raw = Advice([finding.fix], [])
    elif person and _takes_person(fn):
        raw = fn(finding, moment, person=True)
    else:
        raw = fn(finding, moment)
    return Advice(
        [safe_step(step) for step in raw.fix_steps],
        [safe_step(step) for step in raw.ask_human],
    )


# ---- helpers ---------------------------------------------------------------------------------


def _takes_person(fn: Adviser) -> bool:
    try:
        return "person" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def split_location(location: str) -> tuple[str | None, str]:
    """'pkg-1.0.tgz!package/x' -> ('pkg-1.0.tgz', 'package/x'); a plain path -> (None, path)."""
    head, sep, tail = location.partition("!")
    if sep and head.endswith(_PACKAGE_SUFFIXES):
        return head, tail
    return None, location


def agent_path(member: str) -> str:
    """The agent-owned part of a path: 'package/sub/.claude/x.json' -> '.claude/x.json'."""
    for agent in ALL_AGENTS:
        for rule in agent.package_rules:
            match = rule.pattern.search(member)
            if match:
                return member[match.start() :].lstrip("/")
    return member.rsplit("/", 1)[-1]


def _ignore_line(path: str) -> str:
    """Ignore the whole agent folder ('.claude/'), or the single file if it's top level."""
    first, sep, _ = path.partition("/")
    return first + "/" if sep else path


def _where(finding: Finding) -> str:
    line = f" line {finding.line}" if finding.line else ""
    return f"{quote(finding.location)}{line}"


def rotate_text(finding: Finding, *, person: bool = False) -> str:
    """Rotate this finding's key, and where. For an AI agent it says the agent cannot do this;
    for a person (`person`) it is simply their step."""
    name, _, shown = finding.detail.partition(": ")
    shown = shown.partition(HIDDEN_MARKER)[0]
    rule_id = _RULE_BY_NAME.get(name)
    url = ROTATE_AT.get(rule_id or "")
    key = f"the {clean(name)} that starts {quote(shown)}"
    if person:
        tail = "Treat the old key as public."
    else:
        tail = "The agent cannot do this; treat the old key as public."
    if url:
        return f"Rotate {key}: revoke it and create a new one at {url}. {tail}"
    return f"Rotate {key} with the service that issued it (agentprint can't tell which). {tail}"


def _hidden_key(finding: Finding) -> str | None:
    """The start of the step for a key hidden on purpose (split, folded, invisible characters,
    base64), which agentprint cannot mask: None for a key written out plainly."""
    how = hidden_how(finding.detail)
    if how is None:
        return None
    return (
        f"In {_where(finding)}, a key is hidden ({how}): its characters are not stored together, "
        "so agentprint cannot mask it. Find every piece of it and delete them by hand"
    )


def _rotate(finding: Finding, person: bool = False) -> list[str]:
    return [rotate_text(finding, person=person)]


def _no_leak(step: str, person: bool) -> str:
    """A step that handles a key: an AI agent is also told never to show or reuse it."""
    return step if person else f"{step} {NO_LEAK}"


# ---- advisers --------------------------------------------------------------------------------


@adviser("AP001")
def _history_secret(finding: Finding, moment: Moment, person: bool = False) -> Advice:
    hidden = _hidden_key(finding)
    if hidden is not None:
        step = f"{hidden}, or delete the file if that history is not needed."
    else:
        step = (
            f"In {_where(finding)}, replace the key with the text REDACTED, or delete the "
            "file if that history is not needed."
        )
    return Advice([_no_leak(step, person)], _rotate(finding, person))


@adviser("AP002")
def _settings_secret(finding: Finding, moment: Moment, person: bool = False) -> Advice:
    package, member = split_location(finding.location)
    hidden = _hidden_key(finding)
    if package is not None:
        source = quote(agent_path(member))
        how = hidden_how(finding.detail)
        delete = (
            f"In the source file {source} in the project (line {finding.line}), a key is hidden "
            f"({how}): its characters are not stored together, so agentprint cannot mask it. "
            "Find every piece of it and delete them by hand"
            if how is not None
            else f"Delete the key from the source file {source} in the project (line "
            f"{finding.line})"
        )
        steps = [
            _no_leak(
                f"{delete}; the package copy is built from it. Load the key from an environment "
                "variable instead.",
                person,
            ),
            "Also keep that file out of the package (see the matching AP006 finding).",
        ]
    elif moment is Moment.push:
        delete = (
            f"{hidden}, load the key from an environment variable instead"
            if hidden is not None
            else f"Delete the key from {_where(finding)}, load it from an environment variable "
            "instead"
        )
        steps = [
            _no_leak(f"{delete}, and commit the change.", person),
            "The key stays in earlier commits, so rotating it (ask_human) is what makes it safe.",
        ]
    else:
        delete = (
            f"{hidden}, and load the key"
            if hidden is not None
            else f"Delete the key from {_where(finding)} and load it"
        )
        steps = [
            _no_leak(
                f"{delete} from an environment variable instead (set it in your shell profile "
                "or a secret manager).",
                person,
            ),
        ]
    return Advice(steps, _rotate(finding, person))


@adviser("AP004")
def _network_hook(finding: Finding, moment: Moment, person: bool = False) -> Advice:
    # The check's own fix is the step. It asks the human first, which a person reads as their
    # own question: "If you did not add this hook, delete it."
    if person and HOOK_ASK_FIRST in finding.fix:
        return Advice([finding.fix.replace(HOOK_ASK_FIRST, HOOK_PERSON_FIRST, 1)])
    return Advice([finding.fix])


@adviser("AP005")
def _mcp_inline_secret(finding: Finding, moment: Moment, person: bool = False) -> Advice:
    # The check's own fix already names this agent's way to read the key from the environment.
    # It starts by telling the agent to ask the human to rotate; a person gets rotating as a
    # step of its own, after it.
    key_part = finding.detail.partition(MCP_MARKER)[0]
    step = finding.fix
    if person and step.startswith(MCP_ASK_FIRST):
        rest = step.removeprefix(MCP_ASK_FIRST)
        step = rest[:1].upper() + rest[1:]
    return Advice([step], _rotate(replace(finding, detail=key_part), person))


@adviser("AP006")
def _agent_file_shipped(finding: Finding, moment: Moment) -> Advice:
    package, member = split_location(finding.location)
    path = agent_path(member)
    if package is None or moment is Moment.push:
        if clean(member) == member:  # the real path, shown whole: safe to put in a command
            untrack = f"git rm --cached -- {shlex.quote(member)}"
        else:  # a cleaned or shortened path would name some other file
            untrack = "run git rm --cached on that file (it keeps your local copy)"
        return Advice(
            [
                f"Stop tracking {quote(member)} but keep the local file: {untrack}",
                f"Add the line {quote(path)} to .gitignore, commit both changes, then push again.",
            ]
        )
    exclude = quote(_ignore_line(path))
    steps: list[str] = []
    if package.endswith(".tgz"):
        steps.append(
            f"Add the line {exclude} to .npmignore in the package folder. If .npmignore does not "
            "exist yet, first copy .gitignore to .npmignore: npm stops reading .gitignore once "
            '.npmignore exists. If package.json has a "files" list, make sure no entry in it '
            "includes this path."
        )
    else:
        steps.append(
            f"Exclude {exclude} from the package in your build config: hatchling "
            "[tool.hatch.build.targets.sdist] and .wheel exclude = [...], setuptools "
            "MANIFEST.in prune/exclude, poetry exclude = [...], flit [tool.flit.sdist] exclude."
        )
    steps.append(f"Add the line {quote(path)} to .gitignore so it is never committed either.")
    return Advice(steps)


@adviser("AP007")
def _agent_file_not_ignored(finding: Finding, moment: Moment) -> Advice:
    path = agent_path(split_location(finding.location)[1])
    return Advice([f"Add the line {quote(path)} to .gitignore (it holds personal agent data)."])


@adviser("AP008")
def _readable_by_others(finding: Finding, moment: Moment) -> Advice:
    # The check's fix is already the exact, shell-quoted chmod for these files.
    ask = []
    if finding.severity >= Severity.HIGH:
        ask.append(
            "If other people or programs use accounts on this computer, treat the keys in "
            f"{_where(finding)} as exposed and rotate them with the services that issued them."
        )
    return Advice([finding.fix], ask)


OPEN_SOURCE_ASK = (
    "Decide whether this package is meant to be open source; if it is, give it an open-source "
    "licence (an SPDX id such as MIT) and the medium and high prints go away."
)
SPDX_ASK = (
    "If it is open source, use an SPDX licence id (such as MIT or Apache-2.0) as the licence, "
    "or as one OR choice next to the custom one."
)
GUESS_ASK = "Decide whether this package is meant to be open source."
# When "closed source" is only a guess (no licence found, or not readable): how to state one.
_STATE_LICENCE = {
    ap009.NPM_MISSING: 'add a license field (for example "license": "MIT") to package.json',
    ap009.PYPI_MISSING: (
        'add its licence to the package metadata (for example license = "MIT" in the [project] '
        "table of pyproject.toml)"
    ),
}
_STATE_ANY = (
    'make sure it states its licence (a "license" field in package.json, or license in the '
    "[project] table of pyproject.toml)"
)


@adviser("AP009")
def _rebuildable_source(finding: Finding, moment: Moment, person: bool = False) -> Advice:
    # The check's fix for each kind names the build settings (tsc sourceMap, esbuild
    # --sources-content, Vite build.sourcemap, webpack devtool). Nothing here edits a build
    # config. Only the check's own words in the detail are read: the example path after them
    # comes from the package.
    step = finding.fix if finding.fix in ap009.FIXES else ap009.MAP_FIX
    why = ap009.reason(finding.detail)
    if ap009.PRIVATE in why or finding.severity is Severity.LOW:
        # An open-source package's maps, or a private registry: a note, and its fix (already
        # worded as optional by the check) is the whole advice. Nothing to ask the human.
        return Advice([step])
    if any(guess in why for guess in ap009.GUESSED):
        # "Closed" is a guess: a licence may be the whole fix, so it comes before the build.
        how = next((h for g, h in _STATE_LICENCE.items() if g in why), _STATE_ANY)
        first = (
            f"If this package is open source, {how}: that is the whole fix, and the medium and "
            "high prints go away."
        )
        if not person:
            first += " If you cannot tell, ask the human before changing the build."
        steps = [first, f"If it is not open source: {step[0].lower()}{step[1:]}"]
        return Advice(steps, [GUESS_ASK])
    ask = [OPEN_SOURCE_ASK]
    if ap009.CUSTOM_ID in why:
        ask.append(SPDX_ASK)
    return Advice([step], ask)


# ---- explanations (agentprint mcp: explain_finding) ------------------------------------------

EXPLAIN: dict[str, str] = {
    "AP001": (
        "AP001 Secret in agent history (high). An API key sits in a file where an AI agent keeps "
        "its chat history, transcripts, pasted text or backups of files it edited. Those files "
        "are copied by backups and sync tools, read by other programs, and sometimes published "
        "by accident, so the key must be treated as exposed. Fix: the human rotates the key at "
        "the provider; the agent replaces the key in that file with REDACTED or deletes the file."
    ),
    "AP002": (
        "AP002 Secret in agent settings (critical). An API key is written into an agent's "
        "settings, permission allowlist or MCP config. These files are read on every start, often "
        "shared, and can end up in a git commit or a published package. Fix: Rotate the key with "
        "its provider (the human must do this), remove it from the file, and load it from an "
        "environment variable instead."
    ),
    "AP006": (
        "AP006 Agent file would ship in package (high/medium). A file an AI agent writes for "
        "itself (personal allowlist, conversation log, local notes, login token) is inside the "
        "package you are about to publish, where anyone can download it. Fix: exclude it "
        "(.npmignore or the package.json files list for npm; the build tool's exclude list for "
        "Python), add it to .gitignore, and check again. Rotate any key it held."
    ),
    "AP009": (
        "AP009 Package lets an AI rebuild your source (high/medium/low). The package you are "
        "about to publish holds source maps with your original source, source maps that give "
        "away your file layout, original .ts/.vue/.svelte source next to the compiled files, or "
        "AI instruction files. Nobody can stop reverse engineering of code you ship; this shows "
        "what hands an AI your source or a map of it. Severity depends on the licence. Closed "
        "source (high and medium prints): no licence, UNLICENSED, SEE LICENSE IN, a proprietary "
        "marker, or an SPDX expression whose every OR choice is a custom LicenseRef id. Open "
        "source: only maps that hold the full original source are mentioned, as a low note; "
        "nothing is wrong, and sourcesContent can be turned off if that was not meant. A package "
        "whose package.json publishConfig.registry is a private registry gets low notes only. "
        "Fix for closed source: turn off source maps or their sourcesContent in the build (tsc "
        "sourceMap/inlineSources, esbuild --sources-content=false, Vite build.sourcemap, webpack "
        "devtool), or keep the files out with .npmignore, the package.json files list or the "
        "Python build tool's exclude list. When no licence was found, ask the human first: if "
        "the package is open source, adding a licence (an SPDX id such as MIT) is the whole fix "
        "and the medium and high prints go away."
    ),
    "AP007": (
        "AP007 Personal agent file not gitignored (low). A personal agent file is neither "
        "committed nor in .gitignore, so the next `git add .` would commit it. Fix: add it to "
        ".gitignore."
    ),
}


def explain(check_id: str) -> str:
    check_id = clean(check_id).strip().upper()[:20]
    if check_id in EXPLAIN:
        return EXPLAIN[check_id]
    from .checks import SCAN_CHECKS  # checks added later explain themselves via title + fix

    for check in SCAN_CHECKS:
        if getattr(check, "id", None) == check_id:
            severity = getattr(getattr(check, "severity", None), "name", "").lower()
            return f"{check_id} {check.title} ({severity}). Fix: {check.fix}"
    known = ", ".join(sorted(EXPLAIN))
    return f"agentprint has no check called {quote(check_id)}. Known checks: {known}."
