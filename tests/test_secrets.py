import time

import pytest

from agentprint.secrets import find_secrets, redact, shannon_entropy
from tests import fakes


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
    start = time.perf_counter()
    matches = find_secrets(line)
    assert time.perf_counter() - start < 2.0
    assert [m.value for m in matches] == [fakes.GITHUB]
