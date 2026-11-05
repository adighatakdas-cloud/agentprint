"""What to do about each finding, written for the AI agent that will do it.

`fix_steps` are actions an agent can perform itself (edit a file, add an ignore line, untrack a
file). `ask_human` is what only the human can do, such as rotating a key in a provider console.

Checks plug in here, not in the report code: register an adviser for a check id with
`@adviser("AP0xx")`. A check with no adviser falls back to its one-line `fix` hint.
Every value taken from scanned content goes through `sanitize.quote()`.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .agents import ALL_AGENTS
from .checks.ap005_mcp_inline_secret import _MARKER as MCP_MARKER
from .models import Finding
from .sanitize import clean, quote
from .secrets import RULES


class Moment(StrEnum):
    """What the user is about to do; it changes which fix applies."""

    scan = "scan"  # a general check of this machine or project
    publish = "publish"  # about to publish a package
    push = "push"  # about to git push


@dataclass(frozen=True)
class Advice:
    fix_steps: list[str] = field(default_factory=list)
    ask_human: list[str] = field(default_factory=list)


Adviser = Callable[[Finding, Moment], Advice]
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


def adviser(check_id: str) -> Callable[[Adviser], Adviser]:
    def register(fn: Adviser) -> Adviser:
        ADVISERS[check_id] = fn
        return fn

    return register


def advise(finding: Finding, moment: Moment) -> Advice:
    fn = ADVISERS.get(finding.check_id)
    return fn(finding, moment) if fn else Advice([finding.fix], [])


# ---- helpers ---------------------------------------------------------------------------------


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


def _rotate(finding: Finding) -> list[str]:
    name, _, shown = finding.detail.partition(": ")
    rule_id = _RULE_BY_NAME.get(name)
    url = ROTATE_AT.get(rule_id or "")
    key = f"the {clean(name)} that starts {quote(shown)}"
    if url:
        return [
            f"Rotate {key}: revoke it and create a new one at {url}. The agent cannot do this; "
            "treat the old key as public."
        ]
    return [
        f"Rotate {key} with the service that issued it (agentprint can't tell which). The agent "
        "cannot do this; treat the old key as public."
    ]


# ---- advisers --------------------------------------------------------------------------------


@adviser("AP001")
def _history_secret(finding: Finding, moment: Moment) -> Advice:
    return Advice(
        [
            f"In {_where(finding)}, replace the key with the text REDACTED, or delete the file if "
            "that history is not needed. " + NO_LEAK,
        ],
        _rotate(finding),
    )


@adviser("AP002")
def _settings_secret(finding: Finding, moment: Moment) -> Advice:
    package, member = split_location(finding.location)
    if package is not None:
        source = quote(agent_path(member))
        steps = [
            f"Delete the key from the source file {source} in the project (line {finding.line}); "
            "the package copy is built from it. Load the key from an environment variable "
            "instead. " + NO_LEAK,
            "Also keep that file out of the package (see the matching AP006 finding).",
        ]
    elif moment is Moment.push:
        steps = [
            f"Delete the key from {_where(finding)}, load it from an environment variable "
            "instead, and commit the change. " + NO_LEAK,
            "The key stays in earlier commits, so rotating it (ask_human) is what makes it safe.",
        ]
    else:
        steps = [
            f"Delete the key from {_where(finding)} and load it from an environment variable "
            "instead (set it in your shell profile or a secret manager). " + NO_LEAK,
        ]
    return Advice(steps, _rotate(finding))


@adviser("AP005")
def _mcp_inline_secret(finding: Finding, moment: Moment) -> Advice:
    # The check's own fix already names this agent's way to read the key from the environment.
    key_part = finding.detail.partition(MCP_MARKER)[0]
    return Advice([finding.fix], _rotate(replace(finding, detail=key_part)))


@adviser("AP006")
def _agent_file_shipped(finding: Finding, moment: Moment) -> Advice:
    package, member = split_location(finding.location)
    path = agent_path(member)
    if package is None or moment is Moment.push:
        rel = clean(member)
        return Advice(
            [
                f"Stop tracking {quote(member)} but keep the local file: "
                f"git rm --cached -- {shlex.quote(rel)}",
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
