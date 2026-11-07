"""Publishes hidden in the files a command runs, and the word-scan backstop that reads them."""

from pathlib import Path

import pytest

from agentprint import commands
from agentprint.commands import Action
from tests import timing

CWD = Path("/work/app")
N = 64 * 1024  # the most of any one file the guard reads

# ---- the backstop word scan -----------------------------------------------------------------


def _backstop(text):
    return [(r.action, r.tool) for r in commands._backstop(text, CWD)]


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("A=1 B=2 npm publish", [(Action.publish, "npm")]),
        ("TAG=$(git-describe)-rc npm publish", [(Action.publish, "npm")]),
        ("T=${X}x git push", [(Action.push, "git")]),
        ("\n\n  \n\t  npm publish", [(Action.publish, "npm")]),  # after a run of blank lines
        ("(cd x)/npm publish", [(Action.publish, "npm")]),
        ("do  twine upload dist/*", [(Action.publish, "twine")]),
        ("a=b;c npm publish", []),  # `c` is the program here, not npm
        ("D=./bin/twine upload", []),  # an assignment, then a program called `upload`
    ],
)
def test_backstop_reads_command_positions(text, want):
    assert _backstop(text) == want


@pytest.mark.parametrize(
    "text",
    [
        ";a=" * (N // 3),  # was quadratic: each `;` rescanned the rest of the word
        "\n" * N,  # was quadratic: each newline rescanned the whole blank run
        "\n " * (N // 2),
        "A=$(x)" * (N // 6),
        ";A=$(x)" * (N // 7),
        ("A=$(x)" + "a" * 50) * (N // 56),
        "do " * (N // 3),
        "git" + " -a b" * (N // 5),
        "npm" + " -a" * (N // 3),
        "{x}/" * (N // 4),
    ],
    ids=["semicolon-assign", "newlines", "newline-space", "subst", "semicolon-subst",
         "subst-word", "keywords", "git-options", "npm-options", "braces"],
)  # fmt: skip
def test_backstop_is_linear_on_a_64kb_script(text):
    assert timing.best_of(lambda: commands._backstop(text, CWD)) < timing.bound(0.5)
