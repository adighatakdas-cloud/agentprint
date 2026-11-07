"""The guard on publishes hidden in scripts: a real check when it can read the commands, and
"could not check" (a warning, or a block in strict mode) when it cannot."""

import json

import pytest

from agentprint.guard import STRICT_HINT, evaluate
from tests import fakes
from tests.test_guard import LEAKY, _tgz


@pytest.fixture
def leaky(tmp_path):
    _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": LEAKY})
    return tmp_path


POSTVERSION = {"postversion": "npm publish demo-1.0.0.tgz"}


def _script(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("files", "command"),
    [
        ({"release.sh": "#!/bin/sh\nset -e\nnpm publish demo-1.0.0.tgz\n"}, "./release.sh"),
        ({"Makefile": "release:\n\t@npm publish demo-1.0.0.tgz\n"}, "make release"),
        ({"justfile": "release:\n    npm publish demo-1.0.0.tgz\n"}, "just release"),
        ({"package.json": json.dumps({"scripts": POSTVERSION})}, "npm version patch"),
    ],
    ids=["shell-script", "make", "just", "npm-version"],
)
def test_a_leak_published_from_a_script_blocks(leaky, files, command):
    for rel, text in files.items():
        _script(leaky, rel, text)
    verdict = evaluate(command, leaky)
    assert verdict.block
    assert verdict.reason.startswith("agentprint: BLOCK")
    assert command in verdict.reason  # the agent is told what to re-run
    assert fakes.ANTHROPIC not in verdict.reason


def test_a_clean_publish_from_a_script_is_allowed_quietly(tmp_path):
    _tgz(tmp_path / "ok-1.0.0.tgz", {"package/index.js": b"1"})
    _script(tmp_path, "release.sh", "npm publish ok-1.0.0.tgz\n")
    verdict = evaluate("./release.sh", tmp_path)
    assert not verdict.block and verdict.warning is None


def test_a_script_that_does_not_publish_is_not_our_business(tmp_path):
    _script(tmp_path, "build.sh", "#!/bin/sh\n# later: npm publish\nnpm run build\n")
    assert evaluate("./build.sh", tmp_path) is None


RELEASE_PY = 'import subprocess\nsubprocess.run(["twine", "upload", "dist/*"])\n'
COULD_NOT = (
    "agentprint could not check this twine publish: publishes from inside scripts/release.py; "
    "could not check."
)


def test_python_release_script_warns_by_default_and_blocks_in_strict_mode(tmp_path):
    _script(tmp_path, "scripts/release.py", RELEASE_PY)
    default = evaluate("python scripts/release.py", tmp_path)
    assert not default.block and default.warning == COULD_NOT
    strict = evaluate("python scripts/release.py", tmp_path, strict=True)
    assert strict.block
    assert strict.reason == f"agentprint: BLOCK (strict mode)\n{COULD_NOT}\n{STRICT_HINT}\n"


def test_a_publish_named_only_in_a_python_comment_is_never_a_block_by_default(tmp_path):
    _script(tmp_path, "build.py", "# CI runs npm publish after this step\nprint('built')\n")
    verdict = evaluate("python build.py", tmp_path)
    assert not verdict.block and "could not check this npm publish" in verdict.warning


def test_a_script_too_large_to_read_warns_or_blocks_in_strict_mode(tmp_path):
    _script(tmp_path, "configure", "#!/bin/sh\n" + "# padding\n" * 7000)
    default = evaluate("./configure", tmp_path)
    assert not default.block and "larger than 64 KB" in default.warning
    assert evaluate("./configure", tmp_path, strict=True).block


def test_could_not_check_names_the_file_and_the_next_step():
    from pathlib import Path

    from agentprint.commands import Action, Risky
    from agentprint.guard import _could_not
    from agentprint.models import CannotCheck

    item = Risky(
        Action.publish, "sh", Path("/x"), unsure="agentprint could not open it", label="ship.sh"
    )
    assert _could_not(item, CannotCheck(item.unsure)) == (
        "agentprint could not check `ship.sh`: agentprint could not open it. Run the publish or "
        "push as its own command, so agentprint can check it."
    )
    plain = Risky(Action.push, "git", Path("/x"), unsure="its folder is unknown")
    assert _could_not(plain, CannotCheck(plain.unsure)) == (
        "agentprint could not check this git push: its folder is unknown."
    )
