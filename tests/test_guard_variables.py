"""A publish or push named through a variable is "could not check", never nothing."""

from pathlib import Path

import pytest

from agentprint.commands import Action, detect
from agentprint.guard import evaluate


@pytest.mark.parametrize(
    ("command", "action"),
    [
        ('"$NPM" publish', Action.publish),
        ("$NPM publish --tag next", Action.publish),
        ("${PUBLISHER} upload dist/*", Action.publish),
        ('"$GIT" push origin main', Action.push),
        ("npm $CMD", Action.publish),
        ("pnpm $SUBCOMMAND --no-git-checks", Action.publish),
        ("uv $STEP", Action.publish),
        ("git $CMD origin main", Action.push),
        ("git -C pkg $CMD", Action.push),
        ("npm $FLAGS publish", Action.publish),
    ],
)
def test_a_publish_or_push_named_by_a_variable_is_could_not_check(tmp_path: Path, command, action):
    [risky] = detect(command, tmp_path)
    assert risky.action == action
    assert "variable" in risky.unsure and risky.label


@pytest.mark.parametrize(
    "command",
    [
        "git $FLAGS commit -m wip",
        "git $GIT_OPTS status",
        "npm $FLAGS install",
        "npm $FLAGS run build",
        "uv $Q sync",
        '"$PY" -m build',
        '"$EDITOR" notes.md',
        "$CC -o app main.c",
        "echo $NPM publish",  # echo only prints the words
        "git log --format=$FMT",
    ],
)
def test_ordinary_commands_with_variables_stay_quiet(tmp_path: Path, command):
    assert detect(command, tmp_path) == []


def test_a_defaulted_tool_is_checked_as_its_default(tmp_path: Path):
    [risky] = detect("${NPM:-npm} publish demo-1.0.0.tgz", tmp_path)
    assert (risky.action, risky.tool, risky.unsure) == (Action.publish, "npm", "")
    assert risky.targets == ("demo-1.0.0.tgz",)


def test_inside_a_followed_script_too(tmp_path: Path):
    script = tmp_path / "release.sh"
    script.write_text('#!/bin/sh\nset -e\nNPM=npm\n"$NPM" publish\n', newline="\n")
    script.chmod(0o755)
    [risky] = detect("./release.sh", tmp_path)
    assert risky.action == Action.publish and "variable" in risky.unsure


def test_default_mode_warns_and_strict_blocks(tmp_path: Path):
    default = evaluate('"$NPM" publish', tmp_path)
    assert default is not None and not default.block
    strict = evaluate('"$NPM" publish', tmp_path, strict=True)
    assert strict is not None and strict.block
