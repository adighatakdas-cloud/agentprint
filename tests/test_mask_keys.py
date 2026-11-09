"""Text agentprint repeats back never carries a key (CLAUDE.md hard rule 2)."""

from pathlib import Path

import pytest

from agentprint.guard import evaluate
from agentprint.secrets import mask_keys, redact
from tests import fakes
from tests.test_guard import LEAKY, _tgz

NPM = fakes.NEW["npm"]


def _no_key(text: str, key: str) -> None:
    """No run of 5 or more characters of the key survives (the 4-character stub may)."""
    assert not any(key[i : i + 5] in text for i in range(len(key) - 4)), text


@pytest.mark.parametrize(
    "text",
    [
        f"NPM_TOKEN={NPM} npm publish demo-1.0.0.tgz",
        f'cd "$X" && ./release-{NPM}.sh',  # glued after a dash: no word boundary
        f"curl -H 'Authorization: Bearer {fakes.GITHUB}' https://example.invalid",
        f"export KEY={fakes.ANTHROPIC}; npm publish",
    ],
)
def test_a_key_in_echoed_text_is_cut_to_its_stub(text):
    key = next(k for k in (NPM, fakes.GITHUB, fakes.ANTHROPIC) if k in text)
    masked = mask_keys(text)
    _no_key(masked, key)
    assert redact(key) in masked


def test_a_hidden_key_is_cut_too():
    head, tail = fakes.ANTHROPIC[:20], fakes.ANTHROPIC[20:]
    masked = mask_keys(f'KEY="{head}" + "{tail}"; npm publish')
    _no_key(masked, fakes.ANTHROPIC)


def test_text_without_keys_is_unchanged():
    text = "cd pkg && npm publish demo-1.0.0.tgz --tag next # don't"
    assert mask_keys(text) == text


def test_a_block_never_repeats_a_key_typed_into_the_command(tmp_path: Path):
    _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": LEAKY})
    command = f"NPM_TOKEN={NPM} npm publish demo-1.0.0.tgz"
    verdict = evaluate(command, tmp_path)
    assert verdict is not None and verdict.block
    _no_key(verdict.reason, NPM)
    _no_key(verdict.reason, fakes.ANTHROPIC)
