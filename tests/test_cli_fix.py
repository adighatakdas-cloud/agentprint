"""`agentprint fix` on the command line: the plan, when it asks, applying, the re-check, the
three views, --only, --pack and --undo.

Every home and project is a temporary folder: the demo world from demo/build_demo.py, or a few
files written here. HOME points into the temporary folder too, so nothing real is read or
written. Every key is obviously fake (tests.fakes and the demo's "FAKEFAKE..." bodies).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint import __version__, cli
from agentprint.checks import ap008_readable_by_others as ap008
from agentprint.cli import app
from agentprint.integrations.base import INSTRUCTIONS
from agentprint.secrets import find_secrets
from tests import fakes

ROOT = Path(__file__).resolve().parent.parent
BUILD_DEMO = ROOT / "demo" / "build_demo.py"
SNAPSHOT = Path(__file__).parent / "data" / "snapshots" / "fix.agent.txt"

needs_demo = pytest.mark.skipif(not BUILD_DEMO.is_file(), reason="demo/ is not in the sdist")
posix_only = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permissions, non-root",
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fake_real_home(tmp_path, monkeypatch):
    """Path.home() is a temporary folder: a fix run that forgot --home could only touch that."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)


def _build_demo(root: Path) -> tuple[Path, Path]:
    spec = importlib.util.spec_from_file_location("build_demo", BUILD_DEMO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(root)


@pytest.fixture
def world(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """The README demo's fake home and npm project: three keys and a Bash(*) permission."""
    if not BUILD_DEMO.is_file():
        pytest.skip("demo/ is not in the sdist")
    home, project = _build_demo(tmp_path / "world")
    monkeypatch.chdir(tmp_path / "world")
    return home, project


def _files(*roots: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for root in roots
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _fix(world, *args: str, **kwargs):
    home, project = world
    return runner.invoke(app, ["fix", str(project), "--home", str(home), *args], **kwargs)


def _scan(world) -> int:
    home, project = world
    return runner.invoke(app, ["scan", str(project), "--home", str(home)]).exit_code


def _no_key(output: str) -> None:
    assert "FAKEFAKE" not in output, output
    for key in fakes.ALL.values():
        assert key not in output
    assert find_secrets(output) == [], output


# ---- applying ---------------------------------------------------------------------------------


@needs_demo
def test_fix_yes_turns_block_into_clear(world):
    home, project = world
    assert _scan(world) == 1

    result = _fix(world, "--yes")

    assert result.exit_code == 0, result.output
    assert "CLEAR" in result.output and "BLOCK" not in result.output
    assert _scan(world) == 0  # the Bash(*) permission (AP003, medium) stays, as a note
    assert "${GITHUB_TOKEN}" in (home / ".claude.json").read_text()
    assert "FAKEFAKE" not in (home / ".claude" / "history.jsonl").read_text()
    assert "FAKEFAKE" not in (project / ".claude" / "settings.local.json").read_text()
    journals = list((home / ".agentprint" / "fix").glob("*.json"))
    assert len(journals) == 1
    _no_key(journals[0].read_text())


@needs_demo
def test_agent_marker_without_yes_never_prompts_and_applies_nothing(world, monkeypatch):
    home, project = world
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(cli, "is_interactive", lambda *a: True)  # even in a real terminal
    before = _files(home, project)

    result = _fix(world, input="y\n")

    assert result.exit_code == 1
    assert result.output.startswith("agentprint fix: BLOCK")
    assert "re-run with --yes" in result.output
    assert "[y/N]" not in result.output
    assert _files(home, project) == before
    assert not (home / ".agentprint").exists()


@needs_demo
def test_no_tty_without_yes_is_a_dry_run(world):
    home, project = world
    before = _files(home, project)

    result = _fix(world, input="y\n")  # CliRunner gives no terminal

    assert result.exit_code == 1
    assert "re-run with --yes" in result.output
    assert "[y/N]" not in result.output
    assert "BLOCK" in result.output
    assert _files(home, project) == before


@needs_demo
@pytest.mark.parametrize("extra", [[], ["--yes"]])
def test_dry_run_changes_nothing(world, monkeypatch, extra):
    home, project = world
    monkeypatch.setattr(cli, "is_interactive", lambda *a: True)
    before = _files(home, project)

    result = _fix(world, "--dry-run", *extra, input="y\n")

    assert result.exit_code == 1
    assert "dry run" in result.output.lower()
    assert "[y/N]" not in result.output
    assert "mask" in result.output and "env" in result.output
    assert _files(home, project) == before
    assert not (home / ".agentprint").exists()


@needs_demo
def test_a_person_is_asked_and_the_default_is_no(world, monkeypatch):
    home, project = world
    monkeypatch.setattr(cli, "is_interactive", lambda *a: True)
    before = _files(home, project)

    declined = _fix(world, input="\n")

    assert "Apply 3 fixes? [y/N]" in declined.output
    assert declined.exit_code == 1
    assert _files(home, project) == before

    accepted = _fix(world, input="y\n")

    assert accepted.exit_code == 0, accepted.output
    assert "CLEAR" in accepted.output
    assert _scan(world) == 0


@needs_demo
def test_ctrl_d_at_the_question_changes_nothing(world, monkeypatch):
    home, project = world
    monkeypatch.setattr(cli, "is_interactive", lambda *a: True)
    before = _files(home, project)
    result = _fix(world, input="")  # end of input: click aborts the question
    assert result.exit_code == 130
    assert _files(home, project) == before


@needs_demo
def test_human_view_shows_the_plan_and_needs_you(world, monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    result = _fix(world, "--dry-run")
    lines = result.output.splitlines()
    assert lines[0].startswith("agentprint fix  ") and lines[0].endswith("· 3 fixes ready")
    assert "Fixing" in lines and "Needs you" in lines
    verbs = [ln.split()[0] for ln in lines[lines.index("Fixing") + 1 :] if ln.startswith("  ")][:3]
    assert verbs == ["mask", "env", "mask"]
    assert any("${GITHUB_TOKEN}" in ln for ln in lines)
    assert any("Rotate the Anthropic API key" in ln for ln in lines)


# ---- the AI and JSON views ---------------------------------------------------------------------


def _normalise(text: str, tmp_path: Path) -> str:
    return text.replace(str(tmp_path), "<TMP>").replace(__version__, "<VERSION>")


@needs_demo
def test_agent_format_snapshot(world, tmp_path):
    result = _fix(world, "--yes", "--format", "agent")
    assert result.exit_code == 0, result.output
    output = _normalise(result.output, tmp_path)
    assert output.splitlines()[0] == "agentprint fix: CLEAR — 3 fixed, 5 for the human"
    if os.environ.get("AGENTPRINT_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.write_text(output, encoding="utf-8")
        pytest.skip("wrote fix.agent.txt")
    assert output == SNAPSHOT.read_text(encoding="utf-8")


@needs_demo
def test_agent_format_quotes_every_scanned_value(world):
    output = _fix(world, "--yes", "--format", "agent").output
    fixed = [ln for ln in output.splitlines() if ln.startswith(("  - mask ", "  - env "))]
    assert len(fixed) == 3, output
    for line in fixed:
        assert re.fullmatch(r'  - (mask|env) AP00\d "[^"]+" line \d+: .+', line), line
    assert 'MCP server "github"' in output  # a server name is scanned text: quoted


@needs_demo
def test_json_shape(world):
    result = _fix(world, "--yes", "--format", "json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {"applied", "skipped", "needs_human", "after"} <= set(data)
    assert data["verdict"] == "CLEAR"
    assert sorted(c["kind"] for c in data["applied"]) == ["env", "mask", "mask"]
    assert data["skipped"] == []
    assert all(set(c) >= {"kind", "check_id", "path", "line", "shown"} for c in data["applied"])
    assert all(set(m) == {"check_id", "text"} for m in data["needs_human"])
    assert {m["check_id"] for m in data["needs_human"]} == {"AP001", "AP002", "AP003", "AP005"}
    assert data["after"]["summary"] == {"critical": 0, "high": 0, "medium": 1, "low": 0}
    assert [f["check_id"] for f in data["after"]["findings"]] == ["AP003"]


@needs_demo
def test_json_without_yes_lists_the_plan_and_applies_nothing(world):
    result = _fix(world, "--format", "json")
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["verdict"] == "BLOCK" and data["applied"] == []
    assert sorted(c["kind"] for c in data["planned"]) == ["env", "mask", "mask"]


@needs_demo
@pytest.mark.parametrize("fmt", ["table", "json", "agent"])
def test_output_never_contains_a_key(world, fmt, monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    planned = _fix(world, "--format", fmt)
    _no_key(planned.output)
    applied = _fix(world, "--yes", "--format", fmt)
    assert applied.exit_code == 0, applied.output
    _no_key(applied.output)
    again = _fix(world, "--yes", "--format", fmt)  # nothing left to fix: still no key
    _no_key(again.output)


def test_brief_mentions_fix():
    assert "agentprint fix --yes --format agent" in INSTRUCTIONS


# ---- AP008: the check's own file list reaches the plan through the CLI -------------------------


@pytest.fixture
def open_tree(tmp_path, monkeypatch):
    """Folders above tmp_path are private on test machines; count only the ones inside it."""
    top = tmp_path.resolve()
    top.chmod(0o755)
    monkeypatch.setattr(
        ap008, "_parents", lambda path: [p for p in path.parents if p == top or top in p.parents]
    )


def _readable_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    history = home / ".claude" / "history.jsonl"
    history.write_text('{"display": "ls"}\n')
    history.chmod(0o644)
    (home / ".claude").chmod(0o755)
    home.chmod(0o755)
    project = tmp_path / "proj"
    project.mkdir()
    return home, history


@posix_only
def test_ap008_becomes_a_lock_change(tmp_path, open_tree):
    home, history = _readable_home(tmp_path)
    args = ["fix", str(tmp_path / "proj"), "--home", str(home), "--format", "json"]

    planned = json.loads(runner.invoke(app, args).output)
    assert [(c["kind"], c["check_id"]) for c in planned["planned"]] == [("lock", "AP008")]

    result = runner.invoke(app, [*args, "--yes"])

    assert result.exit_code == 0, result.output
    assert [c["kind"] for c in json.loads(result.output)["applied"]] == ["lock"]
    assert stat.S_IMODE(history.stat().st_mode) == 0o600


@posix_only
def test_undo_puts_the_mode_back(tmp_path, open_tree):
    home, history = _readable_home(tmp_path)
    runner.invoke(app, ["fix", str(tmp_path / "proj"), "--home", str(home), "--yes"])
    assert stat.S_IMODE(history.stat().st_mode) == 0o600

    result = runner.invoke(app, ["fix", "--undo", "--home", str(home)])

    assert result.exit_code == 0, result.output
    assert "restored" in result.output
    assert stat.S_IMODE(history.stat().st_mode) == 0o644


@pytest.mark.parametrize("fmt", ["table", "agent", "json"])
def test_undo_without_a_journal_is_a_plain_no_op(tmp_path, fmt):
    result = runner.invoke(app, ["fix", "--undo", "--home", str(tmp_path), "--format", fmt])
    assert result.exit_code == 0, result.output
    assert "nothing to undo" in result.output.lower()
    assert "can't" not in result.output and "not_restored" not in result.output.replace(
        '"not_restored": []', ""
    )
    assert not (tmp_path / ".agentprint").exists()


@needs_demo
def test_undo_says_masked_keys_stay_masked(world):
    home, _ = world
    _fix(world, "--yes")
    result = runner.invoke(app, ["fix", "--undo", "--home", str(home), "--format", "agent"])
    assert result.exit_code == 0
    assert "can't be put back" in result.output
    _no_key(result.output)


# ---- scope and usage ----------------------------------------------------------------------------


@needs_demo
def test_only_narrows_the_plan(world):
    result = _fix(world, "--only", "AP001", "--format", "json")
    data = json.loads(result.output)
    assert [c["check_id"] for c in data["planned"]] == ["AP001"]
    assert {m["check_id"] for m in data["needs_human"]} == {"AP001"}


@needs_demo
def test_only_rejects_an_unknown_check(world):
    result = _fix(world, "--only", "AP001,AP999")
    assert result.exit_code == 2
    assert "AP999" in result.output


def test_missing_path_exits_2(tmp_path):
    result = runner.invoke(app, ["fix", str(tmp_path / "typo"), "--no-home"])
    assert result.exit_code == 2
    assert "does not exist" in result.output


@pytest.mark.parametrize("pack", [False, True])
def test_a_missing_home_is_an_error_and_is_never_created(tmp_path, pack):
    missing = tmp_path / "no-such-home"
    target = _tarball(tmp_path) if pack else tmp_path
    args = ["fix", str(target), "--home", str(missing), "--yes", *(["--pack"] if pack else [])]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert not missing.exists()


def test_undo_cannot_be_combined_with_a_dry_run(tmp_path):
    result = runner.invoke(app, ["fix", "--undo", "--dry-run", "--home", str(tmp_path)])
    assert result.exit_code == 2


def test_nothing_to_fix_is_clear(tmp_path):
    result = runner.invoke(app, ["fix", str(tmp_path), "--no-home", "--yes", "--format", "agent"])
    assert result.exit_code == 0
    assert result.output.startswith("agentprint fix: CLEAR — nothing to fix")
    assert not (tmp_path / ".agentprint").exists()


# ---- --pack --------------------------------------------------------------------------------------


def _tarball(tmp_path: Path) -> Path:
    data = b'{"x": 1}'
    tgz = tmp_path / "p.tgz"
    with tarfile.open(tgz, "w:gz") as tf:
        info = tarfile.TarInfo("package/.claude/settings.local.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return tgz


def test_pack_of_a_built_archive_leaves_it_to_the_human(tmp_path):
    tgz = _tarball(tmp_path)
    before = tgz.read_bytes()
    result = runner.invoke(app, ["fix", str(tgz), "--pack", "--yes", "--format", "json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["planned"] == [] and [m["check_id"] for m in data["needs_human"]] == ["AP006"]
    assert tgz.read_bytes() == before


@needs_demo
@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_pack_fixes_the_npm_project(world):
    _, project = world
    result = runner.invoke(app, ["fix", str(project), "--pack", "--yes", "--format", "agent"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("agentprint fix: CLEAR")
    assert ".claude/" in (project / ".npmignore").read_text().splitlines()
    assert ".claude/" in (project / ".gitignore").read_text().splitlines()
    _no_key(result.output)
    again = runner.invoke(app, ["pack", str(project), "--format", "agent"])
    assert again.exit_code == 0, again.output
