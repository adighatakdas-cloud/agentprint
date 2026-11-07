"""Obvious placeholders (README and config examples) are not keys: not reported, still redacted.

Every placeholder and key is built here at run time from pieces, so no whole key-shaped literal
is in the source.
"""

import base64
import json

import pytest

from agentprint.checks.ap005_mcp_inline_secret import CHECK as AP005
from agentprint.checks.base import safe_snippet
from agentprint.models import Artifact, Kind
from agentprint.secrets import (
    _KEY_PREFIX,
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


def test_every_key_prefix_is_known():
    # The repeated-character test looks at a key's body, after its prefix. A rule whose prefix
    # is missing from _KEY_PREFIX would still be reported, but its x's placeholder would not be
    # recognised. Discord tokens have no fixed prefix; the generic rules have none.
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
    rule_ids = {r.id for r in RULES} - {"generic", "discord"}
    assert rule_ids <= set(keys)
    for rule_id in rule_ids:
        for key in keys[rule_id]:
            prefix = _KEY_PREFIX.match(key)
            assert prefix and 0 < prefix.end() < len(key), rule_id


# A placeholder word counts only where a placeholder puts it: at the start or end of the key's
# body, or next to a separator. Inside a run of random letters and digits it does not, so a
# real key that happens to contain "your" (or "xxxx") is still reported.
def test_a_placeholder_word_inside_a_random_body_does_not_count():
    body = "FAKE" * 4 + "your" + "FAKE" * 4  # 36 characters, "your" glued to letters
    key = "gh" + "p_" + body
    assert [m.value for m in find_secrets(f"T={key}")] == [key]
    key = "gh" + "p_" + "FAKE" * 4 + "1xxxx2" + "FAKE" * 3 + "FA"  # glued to digits
    assert [m.value for m in find_secrets(f"T={key}")] == [key]


@pytest.mark.parametrize(
    "body",
    [
        "your" + "FAKE" * 20,  # starts the body
        "FAKE" * 20 + "your",  # ends it
        "FAKE" * 10 + "-your-" + "FAKE" * 10,  # next to separators
        "FAKE" * 10 + "_example" + "FAKE" * 10,
    ],
)
def test_a_placeholder_word_at_an_edge_or_a_separator_counts(body):
    key = "sk-ant-" + "api03-" + body
    assert find_secrets(f"K={key}", placeholders=True)
    assert find_secrets(f"K={key}") == []
