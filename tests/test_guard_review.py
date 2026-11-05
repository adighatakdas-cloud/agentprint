"""Guard behaviour for workspace publishes, release tools and unknowable commands (review)."""

import json
from pathlib import Path

import pytest

from agentprint import guard
from agentprint.guard import evaluate
from agentprint.pack import PackageListing
from tests import fakes

LEAKY = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}})


def _pkg(root: Path, rel: str, name: str, leaky=False, private=False, **extra) -> Path:
    folder = root / rel
    folder.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "version": "1.0.0", **extra}
    if private:
        data["private"] = True
    (folder / "package.json").write_text(json.dumps(data))
    if leaky:
        (folder / ".claude").mkdir()
        (folder / ".claude" / "settings.local.json").write_text(LEAKY)
    return folder


@pytest.fixture
def fake_npm(monkeypatch):
    """Stand-in for `npm pack --dry-run`: lists every file in the package folder."""
    packed: list[Path] = []

    def read(root: Path, want):
        packed.append(root)
        files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        contents = {f: (root / f).read_bytes() for f in files if want(f)}
        name = json.loads((root / "package.json").read_text())["name"]
        return PackageListing(label=f"{name}-1.0.0.tgz", files=files, contents=contents)

    monkeypatch.setattr(guard, "read_npm_project", read)
    return packed


@pytest.fixture
def monorepo(tmp_path):
    _pkg(tmp_path, ".", "root", private=True, workspaces=["packages/*"])
    _pkg(tmp_path, "packages/a", "@x/a")
    _pkg(tmp_path, "packages/b", "@x/b", leaky=True)
    _pkg(tmp_path, "packages/internal", "@x/internal", private=True, leaky=True)
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        "npm publish --workspaces",
        "pnpm -r publish",
        "yarn workspaces foreach npm publish",
        "npx changeset publish",
        "lerna publish from-package",
    ],
)
def test_every_workspace_package_is_checked(monorepo, fake_npm, command):
    verdict = evaluate(command, monorepo)
    assert verdict.block and "@x/b-1.0.0.tgz" in verdict.reason
    checked = {p.name for p in fake_npm}
    assert {"a", "b"} <= checked and "internal" not in checked  # private ones never publish


def test_pnpm_workspace_yaml_is_read(tmp_path, fake_npm):
    _pkg(tmp_path, ".", "root", private=True)
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'libs/*'\n")
    _pkg(tmp_path, "libs/z", "z", leaky=True)
    assert evaluate("pnpm -r publish", tmp_path).block


@pytest.mark.parametrize("selector", ["b", "packages/b", "@x/b"])
def test_named_workspace_is_the_one_checked(monorepo, fake_npm, selector):
    verdict = evaluate(f"npm publish -w {selector}", monorepo)
    assert verdict.block
    assert [p.name for p in fake_npm] == ["b"]


def test_named_clean_workspace_passes(monorepo, fake_npm):
    assert not evaluate("npm publish --workspace @x/a", monorepo).block


def test_unknown_workspace_is_could_not_check(monorepo, fake_npm):
    verdict = evaluate("npm publish -w nope", monorepo)
    assert not verdict.block and "could not check" in verdict.warning


def test_workspace_publish_without_workspaces_is_could_not_check(tmp_path, fake_npm):
    _pkg(tmp_path, ".", "solo")
    verdict = evaluate("npm publish --workspaces", tmp_path)
    assert not verdict.block and "could not check" in verdict.warning


def test_release_tool_in_single_package_repo_checks_the_package(tmp_path, fake_npm):
    _pkg(tmp_path, ".", "solo", leaky=True)
    for command in ("npx changeset publish", "npx np", "npx semantic-release"):
        assert evaluate(command, tmp_path).block, command


@pytest.mark.parametrize(
    "command",
    [
        'cd "$REPO" && npm publish',
        "pnpm --filter @x/b publish",
        "for p in packages/*; do npm publish $p; done",
    ],
)
def test_unknowable_publish_says_could_not_check(monorepo, fake_npm, command):
    verdict = evaluate(command, monorepo)
    assert not verdict.block and "could not check" in verdict.warning
    assert fake_npm == []  # never "OK" after checking something else


def test_script_that_publishes_is_checked(tmp_path, fake_npm):
    _pkg(tmp_path, ".", "solo", leaky=True, scripts={"release": "npm publish"})
    assert evaluate("npm run release", tmp_path).block
