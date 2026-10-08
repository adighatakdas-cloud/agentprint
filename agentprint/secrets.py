"""Secret detection by pattern. Matches are never validated against any service."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretRule:
    id: str
    name: str
    pattern: re.Pattern[str]
    group: int = 0
    min_entropy: float = 0.0


@dataclass(frozen=True)
class SecretMatch:
    rule_id: str
    name: str
    value: str
    start: int
    end: int


# Specific rules come first; the generic rule only reports text no specific rule claimed.
RULES: tuple[SecretRule, ...] = (
    SecretRule(
        "anthropic",
        "Anthropic API key",
        re.compile(r"sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_\-]{80,120}"),
    ),
    SecretRule(
        "openai",
        "OpenAI API key",
        re.compile(
            r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{40,200}"
            r"|sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"
        ),
    ),
    SecretRule(
        "github",
        "GitHub token",
        re.compile(r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82}"),
    ),
    SecretRule("aws", "AWS access key ID", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    SecretRule("stripe", "Stripe live key", re.compile(r"(?:sk|rk)_live_[A-Za-z0-9]{24,99}")),
    SecretRule("slack", "Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,72}")),
    SecretRule("google", "Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    # Generic rules: a secret-ish name, a real separator, then a random-looking value.
    # Possessive quantifiers (*+) and bounded spacing keep matching linear on hostile input.
    SecretRule(
        "generic",
        "Generic secret",
        re.compile(
            r"(?i)(?<![a-z0-9])(?:api[_-]?key|secret|token|passw(?:or)?d)[\w-]{0,40}+"
            r"\\?[\"']?[ \t]{0,4}[:=][ \t]{0,4}\\?[\"']?([A-Za-z0-9_\-.+]{20,200})"
        ),
        group=1,
        min_entropy=3.5,
    ),
    SecretRule(
        "generic",
        "Generic secret",
        re.compile(r"(?i)(?<![a-z0-9])bearer[ \t]{1,4}([A-Za-z0-9_\-.+]{20,200})"),
        group=1,
        min_entropy=3.5,
    ),
)


def shannon_entropy(s: str) -> float:
    """Bits of randomness per character: ~0 for 'aaaa', 4+ for random keys."""
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _looks_random(value: str, min_entropy: float) -> bool:
    has_digit = any(ch.isdigit() for ch in value)
    has_alpha = any(ch.isalpha() for ch in value)
    return has_digit and has_alpha and shannon_entropy(value) >= min_entropy


def find_secrets(text: str) -> list[SecretMatch]:
    found: list[SecretMatch] = []
    for rule in RULES:
        for m in rule.pattern.finditer(text):
            start, end = m.span(rule.group)
            if any(start < f.end and f.start < end for f in found):
                continue
            value = m.group(rule.group)
            if rule.min_entropy and not _looks_random(value, rule.min_entropy):
                continue
            found.append(SecretMatch(rule.id, rule.name, value, start, end))
    return sorted(found, key=lambda f: f.start)


def redact(value: str) -> str:
    return f"{value[:4]}…(len {len(value)})"
