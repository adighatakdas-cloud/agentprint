from pathlib import Path

import pytest

from agentprint.commands import Action, detect

CWD = Path("/work/app")

# (command, expected [(action, tool, cwd relative to CWD, targets)])
CASES = [
    # npm family
    ("npm publish", [(Action.publish, "npm", ".", ())]),
    ("npm publish --access public", [(Action.publish, "npm", ".", ())]),
    ("npm publish ./dist", [(Action.publish, "npm", ".", ("./dist",))]),
    (
        "npm publish my-pkg-1.0.0.tgz --tag next",
        [(Action.publish, "npm", ".", ("my-pkg-1.0.0.tgz",))],
    ),
    ("npm --prefix web publish", [(Action.publish, "npm", "web", ())]),
    ("pnpm -C packages/a publish", [(Action.publish, "pnpm", "packages/a", ())]),
    ("npm publish 2>&1 | tee log.txt", [(Action.publish, "npm", ".", ())]),
    ("npm publish > out.log 2>/dev/null", [(Action.publish, "npm", ".", ())]),
    ("npm publish &>/dev/null &", [(Action.publish, "npm", ".", ())]),
    ("uv publish --directory pkg", [(Action.publish, "uv", "pkg", ())]),
    ("pnpm publish --no-git-checks", [(Action.publish, "pnpm", ".", ())]),
    ("pnpm --filter @a/b publish", [(Action.publish, "pnpm", ".", ())]),
    ("yarn publish", [(Action.publish, "yarn", ".", ())]),
    ("yarn npm publish", [(Action.publish, "yarn", ".", ())]),
    ("bun publish", [(Action.publish, "bun", ".", ())]),
    ("/usr/local/bin/npm publish", [(Action.publish, "npm", ".", ())]),
    # python family
    ("uv publish", [(Action.publish, "uv", ".", ())]),
    ("uv publish dist/*", [(Action.publish, "uv", ".", ("dist/*",))]),
    ("twine upload dist/*", [(Action.publish, "twine", ".", ("dist/*",))]),
    ("twine upload -r testpypi dist/x.whl", [(Action.publish, "twine", ".", ("dist/x.whl",))]),
    ("python -m twine upload dist/*", [(Action.publish, "twine", ".", ("dist/*",))]),
    (
        "python3 -m twine upload --skip-existing dist/*",
        [(Action.publish, "twine", ".", ("dist/*",))],
    ),
    ("uvx twine upload dist/*", [(Action.publish, "twine", ".", ("dist/*",))]),
    ("pipx run twine upload dist/*", [(Action.publish, "twine", ".", ("dist/*",))]),
    ("poetry publish", [(Action.publish, "poetry", ".", ())]),
    ("poetry publish --build", [(Action.publish, "poetry", ".", ())]),
    ("hatch publish", [(Action.publish, "hatch", ".", ())]),
    ("flit publish", [(Action.publish, "flit", ".", ())]),
    # git push
    ("git push", [(Action.push, "git", ".", ())]),
    ("git push -u origin main", [(Action.push, "git", ".", ())]),
    ("git -C ../other push", [(Action.push, "git", "../other", ())]),
    ("git -c core.x=1 push --force-with-lease", [(Action.push, "git", ".", ())]),
    # composition: cd, env prefixes, wrappers, chains, subshells
    ("cd packages/web && npm publish", [(Action.publish, "npm", "packages/web", ())]),
    ("cd /tmp/x; git push", [(Action.push, "git", "/tmp/x", ())]),
    ("NODE_ENV=production npm publish", [(Action.publish, "npm", ".", ())]),
    ("env NPM_CONFIG_OTP=123 npm publish", [(Action.publish, "npm", ".", ())]),
    ("sudo -E npm publish", [(Action.publish, "npm", ".", ())]),
    ("time npm publish", [(Action.publish, "npm", ".", ())]),
    ("npm test && npm publish", [(Action.publish, "npm", ".", ())]),
    ("npm run build || exit 1; npm publish", [(Action.publish, "npm", ".", ())]),
    ("git add -A && git commit -m 'x' && git push", [(Action.push, "git", ".", ())]),
    ('bash -c "cd lib && npm publish"', [(Action.publish, "npm", "lib", ())]),
    ("sh -lc 'git push origin HEAD'", [(Action.push, "git", ".", ())]),
    ("(cd lib && npm publish)", [(Action.publish, "npm", "lib", ())]),
    ("uv build && uv publish", [(Action.publish, "uv", ".", ())]),
    (
        "npm publish && git push --follow-tags",
        [(Action.publish, "npm", ".", ()), (Action.push, "git", ".", ())],
    ),
    ("npm version patch\nnpm publish", [(Action.publish, "npm", ".", ())]),
    # not risky
    ("npm run publish", []),
    ("npm run build", []),
    ("pnpm run publish:docs", []),
    ("npm install publish", []),
    ("echo npm publish", []),
    ("echo 'git push'", []),
    ("git status", []),
    ("git pushd", []),
    ("grep -r 'npm publish' .", []),
    ("npm pack --dry-run", []),
    ("uv build", []),
    ("twine check dist/*", []),
    ("poetry build", []),
    ("git log --grep=push", []),
    ("", []),
]


@pytest.mark.parametrize(("command", "expected"), CASES, ids=[c for c, _ in CASES])
def test_detect(command, expected):
    got = [(r.action, r.tool, r.cwd, r.targets) for r in detect(command, CWD)]
    want = [(a, tool, (CWD / rel), targets) for a, tool, rel, targets in expected]
    assert got == want


def test_unparseable_command_still_detected_without_cwd_change():
    # unbalanced quote: shlex fails, the fallback still catches the publish
    [risky] = detect("npm publish 'oops", CWD)
    assert risky.action is Action.publish and risky.cwd == CWD


def test_cd_to_home_and_tilde_expand(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    [risky] = detect("cd ~/proj && npm publish", CWD)
    assert risky.cwd == tmp_path / "proj"
