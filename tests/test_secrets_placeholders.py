"""Obvious placeholders (README and config examples) are not keys: not reported, still redacted.

Every placeholder and key is built here at run time from pieces, so no whole key-shaped literal
is in the source.
"""

import base64
import json
import random
import string

import pytest

from agentprint.checks.ap005_mcp_inline_secret import CHECK as AP005
from agentprint.checks.base import safe_snippet
from agentprint.models import Artifact, Kind
from agentprint.secrets import (
    _KEY_PREFIX,
    _OLDER_PREFIXES,
    RULES,
    find_secrets,
    find_secrets_deep,
    hidden_spans,
    is_placeholder,
    redact,
)
from tests import fakes

# name -> (environment variable it sits under, placeholder value)
PLACEHOLDERS = {
    "github x's": ("GITHUB_PERSONAL_ACCESS_TOKEN", "gh" + "p_" + "x" * 36),
    "github capital X's": ("GITHUB_TOKEN", "gh" + "p_" + "X" * 36),
    "slack your-bot-token": ("SLACK_BOT_TOKEN", "xo" + "xb-" + "your-bot-token"),
    "aws X's": ("AWS_ACCESS_KEY_ID", "AK" + "IA" + "X" * 16),
    "aws docs example": ("AWS_ACCESS_KEY_ID", "AK" + "IA" + "IOSFODNN7" + "EXAMPLE"),
    "openai x's": ("OPENAI_API_KEY", "sk-" + "proj-" + "x" * 40),
    "openai x's between dashes": ("OPENAI_API_KEY", "sk-" + "proj-" + "xxxx-" * 10),
    "openai your key": ("OPENAI_API_KEY", "sk-" + "proj-" + "your-openai-key-here-" + "0" * 20),
    "anthropic redacted": ("ANTHROPIC_API_KEY", "sk-ant-" + "api03-" + "REDACTED-" * 10),
    "gitlab changeme": ("GITLAB_TOKEN", "gl" + "pat-" + "changeme" + "_" * 12),
    "stripe sample": ("STRIPE_API_KEY", "sk" + "_live_" + "SAMPLE" + "0" * 20),
    "npm dummy": ("NPM_TOKEN", "npm" + "_" + "dummy" + "0" * 31),
    "google placeholder": ("GOOGLE_API_KEY", "AI" + "za" + "PLACEHOLDER" + "_" * 24),
    "github replace me": ("GITHUB_TOKEN", "gh" + "p_" + "REPLACEME" + "0" * 27),
    "deepseek zeros": ("DEEPSEEK_API_KEY", "sk" + "-" + "0" * 32),
    "slack dashes only": ("SLACK_BOT_TOKEN", "xo" + "xb-" + "-" * 12),
}


def _mcp_line(env: str, value: str) -> str:
    """A .mcp.json as a README shows it, on one line."""
    server = {"command": "npx", "args": ["-y", "@example/mcp"], "env": {env: value}}
    return json.dumps({"mcpServers": {"example": server}})


@pytest.mark.parametrize("name", PLACEHOLDERS)
def test_a_placeholder_matches_a_rule_but_is_not_reported(name):
    env, value = PLACEHOLDERS[name]
    line = f"{env}={value}"
    assert find_secrets(line, placeholders=True)  # key-shaped: a rule matches it ...
    assert find_secrets(line) == []  # ... but it is not reported
    assert find_secrets_deep(line) == []


@pytest.mark.parametrize("name", PLACEHOLDERS)
def test_a_placeholder_in_an_mcp_config_is_not_reported(name, tmp_path):
    env, value = PLACEHOLDERS[name]
    line = _mcp_line(env, value)
    assert find_secrets_deep(line) == []
    f = tmp_path / ".mcp.json"
    f.write_text(line + "\n")
    assert AP005.run([Artifact("claude-code", Kind.SETTINGS, f)]) == []


@pytest.mark.parametrize("name", PLACEHOLDERS)
def test_a_placeholder_is_still_redacted_in_output(name):
    env, value = PLACEHOLDERS[name]
    shown = safe_snippet(f"export {env}={value} && go", 400)
    assert value not in shown and redact(value) in shown


def test_a_hidden_placeholder_is_not_reported_but_is_redacted():
    value = PLACEHOLDERS["github x's"][1]
    split = f'k = "{value[:10]}" + "{value[10:]}"'
    encoded = base64.b64encode(f"token {value}".encode()).decode()
    for text in (split, encoded):
        assert find_secrets_deep(text) == []
        assert hidden_spans(text)  # redaction still sees it
        assert value[10:] not in safe_snippet(text, 400)


@pytest.mark.parametrize(
    "template",
    [
        "gh" + "p_<your-token>",
        "sk-" + "proj-{OPENAI_API_KEY}",
        "xo" + "xb-<token>",
        "AK" + "IA{id}",
    ],
)
def test_bracketed_templates_never_match_a_rule(template):
    assert find_secrets(f"K={template}", placeholders=True) == []


def test_every_fake_key_is_a_key_not_a_placeholder():
    keys = [*fakes.ALL.values(), *fakes.NEW.values(), *(k for _, k in fakes.CONTEXT.values())]
    for key in keys:
        assert not is_placeholder(key), redact(key)  # FAKE bodies are not placeholders


def test_every_rule_has_a_known_key_prefix():
    # The tests on a key's body look at what follows its prefix: a newer rule's `starts`, or an
    # older rule's entry in _OLDER_PREFIXES. A rule with neither would still be reported, but
    # its x's placeholder would not be recognised. Discord tokens have no fixed prefix; the
    # generic rules have none.
    rule_ids = {r.id for r in RULES} - {"generic", "discord"}
    assert {r.id for r in RULES if r.starts} | set(_OLDER_PREFIXES) >= rule_ids
    named = {
        "gitlab-routable": "gitlab",
        "dockerhub-org": "dockerhub",
        "huggingface-org": "huggingface",
        "cloudflare-account": "cloudflare",
        "slack-app": "slack",
    }
    keys: dict[str, list[str]] = {}
    for name, key in [*fakes.ALL.items(), *fakes.NEW.items()]:
        keys.setdefault(named.get(name, name), []).append(key)
    for rule_id, (_, key) in fakes.CONTEXT.items():
        keys.setdefault(rule_id, []).append(key)
    assert rule_ids <= set(keys)
    for rule_id in rule_ids:
        for key in keys[rule_id]:
            prefix = _KEY_PREFIX.match(key)
            assert prefix and 0 < prefix.end() < len(key), rule_id


# A placeholder word glued to random characters does not count, wherever it is: a real key that
# happens to hold "your" or "xxxx" must still be reported. Every drop would be a silent miss.
@pytest.mark.parametrize(
    "body",
    [
        "FAKE" * 4 + "your" + "FAKE" * 4,  # glued to letters
        "FAKE" * 4 + "1xxxx2" + "FAKE" * 3 + "FA",  # glued to digits
        "your" + "FAKE" * 8,  # starts the body, glued on one side
        "FAKE" * 8 + "your",  # ends it
        "dummy" + "FAKE" * 7 + "FAK",  # a word, then more than one repeated character
        "FAKE" * 4 + "xxxxxxx" + "FAKE" * 3 + "F",  # seven x's glued to letters
    ],
)
def test_a_placeholder_word_glued_to_random_characters_does_not_count(body):
    assert len(body) == 36
    key = "gh" + "p_" + body
    assert [m.value for m in find_secrets(f"T={key}")] == [key]


@pytest.mark.parametrize(
    "body",
    [
        "FAKE" * 10 + "-your-" + "FAKE" * 10,  # a whole token: separators on both sides
        "your_" + "FAKE" * 20,  # a whole token at the start
        "FAKE" * 10 + "_example" + "FAKE" * 10,  # a long word next to a separator
        "sample" + "FAKE" * 20,  # a long word at the start
        "FAKE" * 20 + "x" * 8,  # eight x's at the end
        "your" + "0" * 80,  # a word, then one repeated character
    ],
)
def test_a_placeholder_word_as_a_token_or_at_an_edge_counts(body):
    key = "sk-ant-" + "api03-" + body
    assert find_secrets(f"K={key}", placeholders=True)
    assert find_secrets(f"K={key}") == []


# ---- templates after a key head ---------------------------------------------------------------

HEAD = "xo" + "xb-" + "1234567890-"


@pytest.mark.parametrize(
    "template",
    ["<your-token>", "<SLACK_TOKEN>", "{SLACK_TOKEN}", "${SLACK_USER_TOKEN}", "{your_token}"],
)
def test_a_key_head_followed_by_a_template_is_not_reported(template):
    text = f"SLACK_BOT_TOKEN={HEAD}{template}"
    assert find_secrets(text, placeholders=True)
    assert find_secrets(text) == [] and find_secrets_deep(text) == []
    assert HEAD not in safe_snippet(text, 200)  # still masked


@pytest.mark.parametrize(
    "text",
    [
        f"<token>{fakes.SLACK}</token>",  # XML: a closing tag never counts
        f"<example>{fakes.SLACK}</example>",
        f"{fakes.SLACK}<BR>",  # an upper-case tag with no underscore is not a name
        f"{HEAD}<token>",  # a lower-case word that is no placeholder
        f"{HEAD}{{x}}",
    ],
)
def test_a_key_before_other_brackets_is_still_reported(text):
    assert find_secrets(text)


# ---- random keys are never placeholders --------------------------------------------------------

B62 = string.ascii_letters + string.digits
URLSAFE = B62 + "_-"
HEX = "0123456789abcdef"
UPPER = string.ascii_uppercase + string.digits
LOWER = string.ascii_lowercase + string.digits
DIGITS = string.digits

# Each rule's key shape: literal parts and (alphabet, length) parts, as the rule reads them.
SHAPES: dict[str, list] = {
    "anthropic": ["sk-ant-", "api03-", (URLSAFE, 93), "AA"],
    "openai project": ["sk-", "proj-", (URLSAFE, 156)],
    "openai legacy": ["sk-", (B62, 20), "T3Blbk", "FJ", (B62, 20)],
    "github": ["gh", "p_", (B62, 36)],
    "github fine-grained": ["github_", "pat_", (B62 + "_", 82)],
    "aws": ["AK", "IA", (UPPER, 16)],
    "stripe": ["sk", "_live_", (B62, 99)],
    "stripe short": ["rk", "_live_", (B62, 24)],
    "slack": ["xo", "xb-", (DIGITS, 12), "-", (DIGITS, 13), "-", (B62, 24)],
    "slack app": ["xa", "pp-1-A", (UPPER, 10), "-", (DIGITS, 13), "-", (LOWER, 64)],
    "google": ["AI", "za", (URLSAFE, 35)],
    "google-oauth": ["GOC", "SPX-", (URLSAFE, 28)],
    "npm": ["npm", "_", (B62, 36)],
    "pypi": ["py", "pi-AgEIcHlwaS5vcmc", (URLSAFE, 150)],
    "gitlab": ["gl", "pat-", (URLSAFE, 20)],
    "gitlab routable": ["gl", "pat-", (URLSAFE, 27), ".", (LOWER, 9)],
    "dockerhub": ["dckr", "_pat_", (URLSAFE, 36)],
    "huggingface": ["hf", "_", (B62, 34)],
    "huggingface org": ["api", "_org_", (string.ascii_letters, 34)],
    "groq": ["gs", "k_", (B62, 52)],
    "openrouter": ["sk-", "or-v1-", (HEX, 64)],
    "xai": ["xa", "i-", (B62 + "_", 80)],
    "perplexity": ["pp", "lx-", (B62, 48)],
    "replicate": ["r8", "_", (URLSAFE, 37)],
    "supabase-pat": ["sb", "p_", (HEX, 40)],
    "supabase-secret": ["sb_", "secret_", (URLSAFE, 40)],
    "cloudflare": ["cf", "ut_", (B62, 40), (HEX, 8)],
    "digitalocean": ["do", "p_v1_", (HEX, 64)],
    "linear": ["lin", "_api_", (B62, 40)],
    "notion": ["nt", "n_", (DIGITS, 11), (B62, 35)],
    "shopify": ["sh", "pat_", (HEX, 32)],
    "atlassian": ["ATA", "TT3", (URLSAFE + "=", 186)],
    "postman": ["PM", "AK-", (HEX, 24), "-", (HEX, 34)],
    "sendgrid": ["S", "G.", (URLSAFE, 22), ".", (URLSAFE, 43)],
    "doppler": ["dp", ".pt.", (B62, 43)],
    "onepassword": ["ops", "_eyJ", (B62 + "+/", 300)],
    "deepseek": ["sk", "-", (HEX, 32)],
    "twilio": ["S", "K", (HEX, 32)],
    "discord": ["M", (URLSAFE, 24), ".", (URLSAFE, 6), ".", (URLSAFE, 30)],
    "telegram": [(DIGITS, 9), ":AA", (URLSAFE, 33)],
    "generic": [(B62 + "_-.+", 32)],
}


def _random(rng: random.Random, alphabet: str, length: int, n: int) -> list[str]:
    """n random strings of `length` characters from `alphabet`, cheaply: random bytes mapped
    onto the alphabet, the uneven top of the byte range dropped so every character is as
    likely."""
    keep = 256 - 256 % len(alphabet)
    table = bytes(ord(alphabet[b % len(alphabet)]) if b < keep else 0 for b in range(256))
    drop = bytes(range(keep, 256))
    need, out = length * n, b""
    while len(out) < need:
        out += rng.randbytes(2 * (need - len(out)) + 64).translate(table, drop)
    text = out[:need].decode()
    return [text[i : i + length] for i in range(0, need, length)]


def _keys(shape: list, n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    parts = [[p] * n if isinstance(p, str) else _random(rng, *p, n) for p in shape]
    return ["".join(pieces) for pieces in zip(*parts, strict=True)]


def test_every_rule_has_a_random_key_shape():
    assert {name.split(" ")[0] for name in SHAPES} == {r.id for r in RULES}


@pytest.mark.parametrize("per_shape", [2_000, pytest.param(100_000, marks=pytest.mark.integration)])
def test_random_keys_of_every_rule_shape_are_never_placeholders(per_shape):
    dropped = {}
    for seed, (name, shape) in enumerate(SHAPES.items(), start=20261107):
        hits = [k for k in _keys(shape, per_shape, seed) if is_placeholder(k)]
        if hits:
            dropped[name] = (len(hits), redact(hits[0]))  # a stub only, never a whole key
    assert dropped == {}
