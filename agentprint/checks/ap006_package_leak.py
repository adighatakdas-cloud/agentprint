"""AP006: an agent file would ship inside a published package (plus any keys inside it)."""

from __future__ import annotations

from ..agents import ALL_AGENTS
from ..models import Finding, PackageRule
from ..pack import PackageListing
from ..secrets import find_secrets, redact
from .ap002_settings_secret import CHECK as AP002

ID = "AP006"
TITLE = "Agent file would ship in package"
FIX = (
    'Keep it out of the package (.npmignore or the package.json "files" list; your Python '
    "build tool's exclude list) and add it to .gitignore."
)


def _match(name: str) -> PackageRule | None:
    for agent in ALL_AGENTS:
        for rule in agent.package_rules:
            if rule.pattern.search(name):
                return rule
    return None


def is_agent_file(name: str) -> bool:
    return _match(name) is not None


def check_listing(listing: PackageListing) -> list[Finding]:
    findings: list[Finding] = []
    for name in listing.files:
        rule = _match(name)
        if rule is None:
            continue
        location = f"{listing.label}!{name}"
        why = rule.why
        if name in listing.skipped:
            why += " Contents not scanned for keys (a link, too large, or archive limit reached)."
        findings.append(Finding(ID, rule.severity, TITLE, rule.agent, location, None, why, FIX))
        text = listing.contents.get(name, b"").decode("utf-8", errors="replace")
        seen: set[str] = set()
        for match in find_secrets(text):
            if match.value in seen:
                continue
            seen.add(match.value)
            findings.append(
                Finding(
                    check_id=AP002.id,
                    severity=AP002.severity,
                    title="Secret in agent file inside package",
                    agent=rule.agent,
                    location=location,
                    line=text.count("\n", 0, match.start) + 1,
                    detail=f"{match.name}: {redact(match.value)}",
                    fix=AP002.fix,
                )
            )
    return findings
