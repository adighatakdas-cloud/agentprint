"""agentprint fix, part 2: env references (AP005), ignore lines (AP007), file modes (AP008), undo.

Every home and project is a temporary folder; every key comes from tests.fakes.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agentprint import fix
from agentprint.checks import ap008_readable_by_others as ap008
from agentprint.scanner import scan
from tests import fakes

posix_only = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permissions, non-root",
)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, newline="\n")
    if mode is not None:
        path.chmod(mode)
    return path


def _ids(findings, check_id):
    return [f for f in findings if f.check_id == check_id]


def _fix(home, project=None):
    p = fix.plan(scan(home, project))
    return p, fix.apply(p, home)


def _claude_json(home: Path, servers: dict, mode: int = 0o640) -> Path:
    doc = {"numStartups": 3, "theme": "dark", "mcpServers": servers}
    return _write(home / ".claude.json", json.dumps(doc, indent=2) + "\n", mode)


# ---- AP005 --------------------------------------------------------------------------------


def test_claude_code_mcp_env_becomes_reference(tmp_path):
    home = tmp_path / "home"
    servers = {"github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": fakes.GITHUB, "X": "1"}}}
    path = _claude_json(home, servers)
    before = json.loads(path.read_text())
    assert _ids(scan(home, None), "AP005")

    p, outcome = _fix(home)

    assert [c.kind for c in p.changes] == ["env"]
    assert len(outcome.applied) == 1 and not outcome.skipped
    after = json.loads(path.read_text())
    assert after["mcpServers"]["github"]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
    before["mcpServers"]["github"]["env"]["GITHUB_TOKEN"] = "${GITHUB_TOKEN}"
    assert after == before
    assert os.name == "nt" or stat.S_IMODE(path.stat().st_mode) == 0o640
    assert any("set GITHUB_TOKEN" in m.text for m in p.manual)
    assert not _ids(scan(home, None), "AP005")
    assert not [f for f in path.parent.rglob("*") if f.name.startswith(".") and "tmp" in f.name]
    journal = outcome.journal.read_text()
    assert fakes.GITHUB not in journal and fakes.GITHUB not in repr(p)
    assert not list((home / ".agentprint").rglob("backups"))  # a key file gets no backup


def test_project_mcp_json_env_becomes_reference(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    doc = {"mcpServers": {"s.v": {"env": {"API_KEY": fakes.OPENAI}}, "t": {"env": {"A": "b"}}}}
    path = _write(project / ".mcp.json", json.dumps(doc))
    home.mkdir()
    _fix(home, project)
    after = json.loads(path.read_text())
    assert after["mcpServers"]["s.v"]["env"]["API_KEY"] == "${API_KEY}"
    assert after["mcpServers"]["t"] == doc["mcpServers"]["t"]


def test_mcp_header_or_other_agent_is_manual(tmp_path):
    home = tmp_path / "home"
    header = _claude_json(
        home, {"web": {"url": "https://x.invalid", "headers": {"X": fakes.GITHUB}}}
    )
    codex = _write(
        home / ".codex" / "config.toml",
        f'[mcp_servers.gh]\ncommand = "x"\n[mcp_servers.gh.env]\nGH_TOKEN = "{fakes.OPENAI}"\n',
    )
    snap = (header.read_bytes(), codex.read_bytes())
    assert len(_ids(scan(home, None), "AP005")) == 2

    p, outcome = _fix(home)

    assert [c for c in p.changes if c.kind == "env"] == []
    assert not outcome.applied
    assert (header.read_bytes(), codex.read_bytes()) == snap
    assert len([m for m in p.manual if m.check_id == "AP005"]) == 2


def test_rewrite_refuses_if_file_changed(tmp_path):
    home = tmp_path / "home"
    path = _claude_json(home, {"g": {"env": {"GITHUB_TOKEN": fakes.GITHUB}}})
    p = fix.plan(scan(home, None))
    assert [c.kind for c in p.changes] == ["env"]
    path.write_text(path.read_text() + "\n")  # the agent saved its own state meanwhile

    outcome = fix.apply(p, home)

    assert not outcome.applied
    assert [reason for _, reason in outcome.skipped] == [fix.CHANGED]
    assert fakes.GITHUB in path.read_text()


# ---- one file, two kinds of change: masks, then an env reference (review I2) ------------------


def _claude_json_mask_and_env(home: Path) -> Path:
    """~/.claude.json with a key in an MCP env block (env) and, after it in the file, another
    in an allowedTools entry (mask): an env rewrite first would move the masked key's bytes."""
    doc = {
        "theme": "dark",
        "mcpServers": {"github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": fakes.GITHUB}}},
        "projects": {"/work/app": {"allowedTools": [f"Bash(curl -u {fakes.STRIPE}: x.test)"]}},
    }
    return _write(home / ".claude.json", json.dumps(doc, indent=2) + "\n", 0o600)


@pytest.mark.parametrize("order", ["as planned", "env first"])
def test_mask_and_env_in_one_file_both_apply(tmp_path, order):
    home = tmp_path / "home"
    path = _claude_json_mask_and_env(home)
    p = fix.plan(scan(home, None))
    assert [(c.kind, c.path) for c in p.changes] == [("mask", path), ("env", path)]
    if order == "env first":  # apply() still masks first: a mask keeps the byte offsets
        p = fix.Plan(list(reversed(p.changes)), p.manual, p.findings)

    outcome = fix.apply(p, home)

    assert outcome.skipped == []
    assert sorted(c.kind for c in outcome.applied) == ["env", "mask"]
    after = json.loads(path.read_text())
    assert after["mcpServers"]["github"]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
    masked = fakes.mask_for_test(fakes.STRIPE)
    assert after["projects"]["/work/app"]["allowedTools"] == [f"Bash(curl -u {masked}: x.test)"]
    assert after["theme"] == "dark"
    assert not [f for f in scan(home, None) if f.check_id in ("AP002", "AP005")]


def test_env_still_refuses_a_change_someone_else_made_after_the_masks(tmp_path, monkeypatch):
    home = tmp_path / "home"
    path = _claude_json_mask_and_env(home)
    p = fix.plan(scan(home, None))
    masks = fix.APPLIERS["mask"]

    def mask_then_claude_code_saves(changes, journal):
        results = masks(changes, journal)
        with changes[0].path.open("a") as fh:
            fh.write("\n")
        return results

    monkeypatch.setitem(fix.APPLIERS, "mask", mask_then_claude_code_saves)
    outcome = fix.apply(p, home)

    assert [c.kind for c in outcome.applied] == ["mask"]
    assert [(c.kind, why) for c, why in outcome.skipped] == [("env", fix.CHANGED)]
    assert fakes.GITHUB in path.read_text()


# ---- F3: prompt history inside ~/.claude.json ----------------------------------------------


def test_claude_json_prompt_history_is_masked_in_place(tmp_path):
    home = tmp_path / "home"
    doc = {
        "theme": "dark",
        "projects": {
            "/work/app": {
                "allowedTools": [],
                "history": [
                    {"display": f"export TOKEN={fakes.GITHUB}", "pastedContents": {}},
                    {"display": "ls", "pastedContents": {"1": {"id": 1, "content": fakes.OPENAI}}},
                ],
            }
        },
    }
    path = _write(home / ".claude.json", json.dumps(doc, indent=2), 0o600)
    assert _ids(scan(home, None), "AP002")

    p, outcome = _fix(home)

    assert {c.kind for c in p.changes} == {"mask"} and len(outcome.applied) == 2
    after = json.loads(path.read_text())
    assert after["theme"] == "dark"
    hist = after["projects"]["/work/app"]["history"]
    assert fakes.GITHUB[:4] + "*" in hist[0]["display"]
    assert hist[1]["pastedContents"]["1"]["content"] == fakes.mask_for_test(fakes.OPENAI)
    assert not _ids(scan(home, None), "AP002")
    assert len(path.read_bytes()) == len(json.dumps(doc, indent=2))


# ---- AP007 --------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root / ".claude" / "settings.local.json", "{}\n")
    (tmp_path / "home").mkdir()
    return root


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@needs_git
def test_gitignore_line_added_with_backup(repo, tmp_path):
    home, gi = tmp_path / "home", repo / ".gitignore"
    gi.write_text("node_modules/\n")
    gi.chmod(0o644)
    assert _ids(scan(None, repo), "AP007")

    p = fix.plan(scan(None, repo))
    outcome = fix.apply(p, home)

    assert [c.kind for c in p.changes] == ["ignore"]
    assert len(outcome.applied) == 1, outcome.skipped
    assert gi.read_text() == "node_modules/\n/.claude/settings.local.json\n"
    assert os.name == "nt" or stat.S_IMODE(gi.stat().st_mode) == 0o644
    backups = [f for f in (home / ".agentprint" / "fix" / "backups").rglob("*") if f.is_file()]
    assert len(backups) == 1 and backups[0].read_text() == "node_modules/\n"
    assert outcome.journal.stem in backups[0].parts
    if sys.platform != "win32":
        assert os.name == "nt" or stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        folders = [p for p in backups[0].parents if outcome.journal.stem in p.parts]
        assert (
            os.name == "nt"
            or folders
            and all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in folders)
        )
    assert not _ids(scan(None, repo), "AP007")
    assert fix.plan(scan(None, repo)).changes == []  # idempotent


@needs_git
def test_gitignore_created_when_missing_has_no_backup(repo, tmp_path):
    home = tmp_path / "home"
    _fix(home, repo)
    assert (repo / ".gitignore").read_text() == "/.claude/settings.local.json\n"
    assert not (home / ".agentprint" / "fix" / "backups").exists()


@needs_git
def test_committed_personal_file_is_manual(repo, tmp_path):
    _git(repo, "add", "-f", ".claude/settings.local.json")
    p = fix.plan(scan(None, repo))
    assert p.changes == []
    assert any("git rm --cached -- .claude/settings.local.json" in m.text for m in p.manual)
    assert not (repo / ".gitignore").exists()


# ---- AP008 --------------------------------------------------------------------------------


@pytest.fixture
def open_tree(tmp_path, monkeypatch):
    top = tmp_path.resolve()
    top.chmod(0o755)
    monkeypatch.setattr(
        ap008, "_parents", lambda path: [p for p in path.parents if p == top or top in p.parents]
    )


@posix_only
def test_lock_sets_go_rwx_and_journals_old_mode(tmp_path, open_tree):
    home = tmp_path / "home"
    hist = _write(home / ".claude" / "history.jsonl", '{"display": "ls"}\n', 0o644)
    other = _write(home / ".claude" / "settings.json", "{}\n", 0o600)
    (home / ".claude").chmod(0o755)
    assert _ids(scan(home, None), "AP008")

    p, outcome = _fix(home)

    assert [c.kind for c in p.changes] == ["lock"]
    assert stat.S_IMODE(hist.stat().st_mode) == 0o600
    assert stat.S_IMODE(other.stat().st_mode) == 0o600
    entry = json.loads(outcome.journal.read_text())["changes"][0]
    assert entry["kind"] == "lock" and entry["undo"]["mode"] == 0o644
    assert not _ids(scan(home, None), "AP008")


@posix_only
def test_lock_keeps_the_owner_execute_bit(tmp_path, open_tree):
    home = tmp_path / "home"
    hook = _write(home / ".claude" / "history.jsonl", "x\n", 0o755)
    _fix(home)
    assert stat.S_IMODE(hook.stat().st_mode) == 0o700


# ---- undo ---------------------------------------------------------------------------------


@needs_git
@posix_only
def test_undo_restores_ignore_and_modes_but_not_masks(repo, tmp_path, open_tree):
    home = tmp_path / "home"
    gi = repo / ".gitignore"
    gi.write_text("dist/\n")
    hist = _write(
        home / ".claude" / "history.jsonl", json.dumps({"display": fakes.GITHUB}) + "\n", 0o644
    )
    (home / ".claude").chmod(0o755)
    p = fix.plan(scan(home, repo))
    assert {c.kind for c in p.changes} == {"mask", "ignore", "lock"}
    fix.apply(p, home)
    assert fakes.GITHUB not in hist.read_text()

    restored, cannot = fix.undo(home)

    assert gi.read_text() == "dist/\n"
    assert stat.S_IMODE(hist.stat().st_mode) == 0o644
    assert fakes.GITHUB not in hist.read_text()
    assert len(restored) == 3 and len(cannot) == 1  # .gitignore, two modes; the mask
    assert "mask" in cannot[0] or "key" in cannot[0]
    assert fakes.GITHUB not in " ".join(restored + cannot)
    again, _ = fix.undo(home)
    assert again == []  # nothing is undone twice


@needs_git
def test_undo_leaves_a_gitignore_edited_since(repo, tmp_path):
    home = tmp_path / "home"
    gi = repo / ".gitignore"
    gi.write_text("dist/\n")
    _fix(home, repo)
    gi.write_text(gi.read_text() + "mine\n")
    restored, cannot = fix.undo(home)
    assert restored == [] and len(cannot) == 1
    assert "mine" in gi.read_text()


@needs_git
@posix_only
def test_a_run_that_changed_nothing_leaves_undo_to_the_run_before(repo, tmp_path):
    """Review I3: a run whose only write failed used to leave an empty newest journal (and a
    backup), and --undo then found nothing to put back."""
    home, gi = tmp_path / "home", repo / ".gitignore"
    gi.write_text("node_modules/\n")
    first = fix.apply(fix.plan(scan(None, repo)), home)
    assert len(first.applied) == 1 and first.journal is not None

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _write(other / ".claude" / "settings.local.json", "{}\n")
    _write(other / ".gitignore", "dist/\n")
    p = fix.plan(scan(None, other))
    assert [c.kind for c in p.changes] == ["ignore"]
    other.chmod(0o555)  # the ignore file can't be replaced
    try:
        second = fix.apply(p, home)
    finally:
        other.chmod(0o755)

    assert second.applied == [] and second.journal is None
    [(_, reason)] = second.skipped
    assert "could not write" in reason
    assert (other / ".gitignore").read_text() == "dist/\n"
    folder = home / ".agentprint" / "fix"
    assert [f.name for f in folder.glob("*.json")] == [first.journal.name]
    assert [d.name for d in (folder / "backups").iterdir()] == [first.journal.stem]

    restored, cannot = fix.undo(home)

    assert len(restored) == 1 and cannot == []
    assert gi.read_text() == "node_modules/\n"


def test_undo_without_journal_is_a_clear_no_op(tmp_path):
    restored, cannot = fix.undo(tmp_path)
    assert restored == []
    assert len(cannot) == 1 and "nothing to undo" in cannot[0]
    assert not (tmp_path / ".agentprint").exists()
