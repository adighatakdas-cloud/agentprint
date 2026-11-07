import pytest

from agentprint.secrets import find_secrets, redact, shannon_entropy
from tests import fakes
from tests.timing import best_of, bound


@pytest.mark.parametrize(("rule_id", "key"), sorted(fakes.ALL.items()))
def test_each_known_key_type_is_found(rule_id, key):
    matches = find_secrets(f'{{"display": "here you go {key} thanks"}}')
    assert [(m.rule_id, m.value) for m in matches] == [(rule_id, key)]


def test_generic_assignment_is_found():
    matches = find_secrets(fakes.GENERIC_LINE)
    assert [(m.rule_id, m.value) for m in matches] == [("generic", fakes.GENERIC_VALUE)]


def test_bearer_header_with_known_key_reports_once():
    matches = find_secrets(f"curl -H 'Authorization: Bearer {fakes.OPENAI}' https://x")
    assert [m.rule_id for m in matches] == ["openai"]


@pytest.mark.parametrize(
    "text",
    [
        '"token_count": 123456',
        '"sessionId": "5d0c1a52-8f1e-4b7a-9c3d-2e6f0a1b9c8d"',
        "the secret is out",
        "password: hunter2",
        "api_key = YOUR_API_KEY_GOES_HERE_PLEASE",
        "tokenizers/bert-base-uncased-v2/vocab.txt",
        "secretsmanager.us-east-1.amazonaws.com",
        "passwordResetTokenExpiresAt2",
        "TokenBucketRateLimiter2Impl.java",
        "/opt/plugins/secret-scanner/1.2.0/skills/SKILL.md",
        "git checkout feature/token-refresh-JIRA-1234-fix",
    ],
)
def test_ordinary_text_is_not_a_secret(text):
    assert find_secrets(text) == []


def test_redact_keeps_four_chars_and_length():
    assert redact(fakes.ANTHROPIC) == f"sk-a…(len {len(fakes.ANTHROPIC)})"
    assert fakes.ANTHROPIC not in redact(fakes.ANTHROPIC)


def test_entropy_ranks_random_above_repetitive():
    assert shannon_entropy("aaaaaaaa") == 0.0
    assert shannon_entropy(fakes.GENERIC_VALUE) > 3.5


def test_long_line_is_fast():
    line = ("x" * 1000 + " ") * 5000 + fakes.GITHUB  # ~5 MB, one line
    found: list = []
    assert best_of(lambda: found.append(find_secrets(line))) < bound(2.0)
    matches = found[0]
    assert [m.value for m in matches] == [fakes.GITHUB]


HEX = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"


@pytest.mark.parametrize(
    "text",
    [
        '{\\"api_key\\": \\"' + HEX + '\\"}',  # JSON escaped inside a transcript line
        "secret_key_base: " + HEX,
        "AWS_SECRET_ACCESS_KEY=" + HEX,
        "Authorization: Bearer " + HEX,
    ],
)
def test_generic_finds_assignments_in_common_shapes(text):
    assert [m.value for m in find_secrets(text)] == [HEX]


def test_padded_keyword_is_fast():
    def run():
        find_secrets("token" + " " * 40000 + "x")
        find_secrets("tokentoken" * 50000)

    assert best_of(run) < bound(2.0)
