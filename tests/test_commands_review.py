"""Detection gaps found in review (reviewer's detect_cases.py / detect2.py), as table tests."""

import json
from pathlib import Path

import pytest

from agentprint.commands import Action, detect

CWD = Path("/work/app")
P, G = Action.publish, Action.push


def _one(command, cwd=CWD):
    found = detect(command, cwd)
    assert len(found) == 1, (command, found)
    return found[0]


# (command, action, tool, all_workspaces, workspaces)
CAUGHT = [
    ("pnpm -r publish", P, "pnpm", True, ()),
    ("pnpm --recursive publish", P, "pnpm", True, ()),
    ("pnpm publish -r", P, "pnpm", True, ()),
    ("pnpm -w publish", P, "pnpm", False, ()),  # -w = workspace root for pnpm, not a value
    ("npm -w pkg publish", P, "npm", False, ("pkg",)),
    ("npm publish -w pkg", P, "npm", False, ("pkg",)),
    ("npm --workspace=pkg publish", P, "npm", False, ("pkg",)),
    ("npm publish --workspace pkg-a --workspace pkg-b", P, "npm", False, ("pkg-a", "pkg-b")),
    ("npm publish --workspaces", P, "npm", True, ()),
    ("yarn workspace foo npm publish", P, "yarn", False, ("foo",)),
    ("yarn workspaces foreach npm publish", P, "yarn", True, ()),
    ("corepack pnpm publish", P, "pnpm", False, ()),
    ("{ npm publish; }", P, "npm", False, ()),
    ("! git push", G, "git", False, ()),
    ("npm test && { git push; }", G, "git", False, ()),
    ("while false; do git push; done", G, "git", False, ()),
    ("if npm test; then git push; fi", G, "git", False, ()),
    ("if true; then npm publish; fi", P, "npm", False, ()),
    ("if false; then :; else git push; fi", G, "git", False, ()),
    ("timeout 120 git push", G, "git", False, ()),
    ("timeout -s KILL 60 npm publish", P, "npm", False, ()),
    ("gtimeout 60 npm publish", P, "npm", False, ()),
    ("nohup npm publish", P, "npm", False, ()),
    ("watch -n 5 git push", G, "git", False, ()),
    ("xargs -n 1 npm publish", P, "npm", False, ()),
    ("eval 'npm publish'", P, "npm", False, ()),
    ("npx changeset publish", P, "changeset", True, ()),
    ("pnpm changeset publish", P, "changeset", True, ()),
    ("pnpm exec changeset publish", P, "changeset", True, ()),
    ("npx lerna publish from-package", P, "lerna", True, ()),
    ("lerna publish", P, "lerna", True, ()),
    ("npx np", P, "np", False, ()),
    ("np --no-tests", P, "np", False, ()),
    ("npm exec -- np", P, "np", False, ()),
    ("npx semantic-release", P, "semantic-release", False, ()),
    ("echo `npm publish`", P, "npm", False, ()),
    ("echo $(npm publish)", P, "npm", False, ()),
    ('out="$(git push 2>&1)"', G, "git", False, ()),
    ("f() { npm publish; }; f", P, "npm", False, ()),
]


@pytest.mark.parametrize(("command", "action", "tool", "every", "names"), CAUGHT)
def test_caught(command, action, tool, every, names):
    risky = _one(command)
    assert (risky.action, risky.tool) == (action, tool)
    assert risky.all_workspaces is every
    assert risky.workspaces == names


NOT_RISKY = [
    "git stash push",
    'git commit -m "push notifications"',
    "git commit -m 'fix: npm publish flow' && echo done",
    "rg 'npm publish' .",
    "grep -rn 'git push' docs/",
    "echo npm publish",
    "echo 'twine upload dist/*'",
    "git log --oneline | grep push",
    "npm publish --dry-run",  # Ruling: a dry run uploads nothing
    "git push --dry-run",
    "git push -n",
    "pnpm publish --dry-run -r",
    "npm run build",
    "pnpm build",
    # Heredoc bodies: see test_commands_scripts.py (data unless fed to a shell).
]


@pytest.mark.parametrize("command", NOT_RISKY)
def test_not_risky(command):
    assert detect(command, CWD) == [], command


@pytest.mark.parametrize(
    "command",
    [
        'cd "$REPO" && git push',
        "for d in a b; do (cd $d && npm publish); done",
        "for p in packages/*; do npm publish $p; done",
        "pnpm -r --filter ./packages/* publish",
        "pnpm --filter @a/b publish",
    ],
)
def test_unknowable_publishes_are_flagged_unsure(command):
    risky = _one(command)
    assert risky.unsure  # guard turns this into "could not check", never "OK"


@pytest.mark.parametrize(
    ("command", "tool"),
    [
        ("npm run release", "npm"),
        ("npm run-script release", "npm"),
        ("yarn release", "npm"),
        ("yarn run release", "npm"),
        ("pnpm release", "npm"),
        ("pnpm run release", "npm"),
        ("bun run release", "npm"),
        ("npm run ship", "npm"),
    ],
)
def test_package_scripts_that_publish_are_followed(tmp_path, command, tool):
    scripts = {"release": "npm version patch && npm publish", "ship": "npm run release"}
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "scripts": scripts}))
    risky = _one(command, tmp_path)
    assert (risky.action, risky.tool, risky.cwd) == (P, tool, tmp_path)


def test_scripts_that_do_not_publish_are_not_flagged(tmp_path):
    scripts = {"build": "tsc", "loop": "npm run loop"}  # self-reference must not recurse forever
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    assert detect("npm run build", tmp_path) == []
    assert detect("npm run loop", tmp_path) == []
