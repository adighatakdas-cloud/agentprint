"""agentprint fix --pack: AP006 in npm packages, AP002 inside packaged agent files.

Every project is a temporary folder; every key comes from tests.fakes.
"""

import json
import shutil
from pathlib import Path

import pytest

from agentprint import fix
from agentprint.checks.ap006_package_leak import check_listing, is_agent_file
from agentprint.pack import PackageListing, collect
from tests import fakes


def _project(tmp_path: Path, package: dict | None = None) -> Path:
    root = tmp_path / "my-app"
    root.mkdir()
    pkg = {"name": "my-app", "version": "1.0.0", **(package or {})}
    (root / "package.json").write_text(json.dumps(pkg))
    (root / "index.js").write_text("module.exports = 1;\n")
    local = {"permissions": {"allow": [f"Bash(curl -H 'Authorization: Bearer {fakes.OPENAI}' *)"]}}
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text(json.dumps(local, indent=2) + "\n")
    return root


def _findings(root: Path):
    return [f for listing in collect(root, is_agent_file) for f in check_listing(listing)]


def _fix(root: Path, home: Path):
    p = fix.plan(_findings(root), pack=True, root=root)
    return p, fix.apply(p, home)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_npm_package_gets_npmignore_from_gitignore(tmp_path):
    root = _project(tmp_path)
    (root / ".gitignore").write_text("node_modules/\n")
    p, outcome = _fix(root, tmp_path / "home")
    assert not outcome.skipped
    npmignore = (root / ".npmignore").read_text()
    # The file itself, from the project root: never the whole .claude/ folder (C1).
    assert npmignore.splitlines() == ["node_modules/", "/.claude/settings.local.json"]
    assert (root / ".gitignore").read_text().splitlines() == [
        "node_modules/",
        "/.claude/settings.local.json",
    ]
    assert (tmp_path / "home/.agentprint/fix").is_dir()


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_existing_npmignore_appended_once(tmp_path):
    root = _project(tmp_path)
    (root / ".npmignore").write_text("test/\n")
    _fix(root, tmp_path / "home")
    assert (root / ".npmignore").read_text() == "test/\n/.claude/settings.local.json\n"
    # a second pass finds nothing left to do
    assert not [c for c in fix.plan(_findings(root), pack=True, root=root).changes]
    assert (root / ".npmignore").read_text() == "test/\n/.claude/settings.local.json\n"


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_files_list_is_manual(tmp_path):
    root = _project(tmp_path, {"files": [".claude", "index.js"]})
    before = (root / "package.json").read_bytes()
    p = fix.plan(_findings(root), pack=True, root=root)
    assert not [c for c in p.changes if c.path.name == ".npmignore"]
    assert any(m.check_id == "AP006" and "files" in m.text for m in p.manual)
    fix.apply(p, tmp_path / "home")
    assert (root / "package.json").read_bytes() == before
    assert not (root / ".npmignore").exists()


def test_pypi_leak_is_manual(tmp_path):
    listing = PackageListing(
        "demo-0.1.0-py3-none-any.whl",
        [".claude/settings.local.json"],
        {".claude/settings.local.json": b"{}"},
    )
    findings = check_listing(listing)
    p = fix.plan(findings, pack=True, root=tmp_path)
    assert not p.changes
    assert any(m.check_id == "AP006" and "hatchling" in m.text for m in p.manual)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_after_fix_pack_is_clear(tmp_path):
    root = _project(tmp_path)
    assert _findings(root)
    p, outcome = _fix(root, tmp_path / "home")
    assert outcome.applied
    assert _findings(root) == []
    # the key in the source file was masked too (allowlist entry), and appears nowhere in output
    text = (root / ".claude" / "settings.local.json").read_text()
    assert fakes.OPENAI not in text
    assert fakes.OPENAI not in repr(p) + json.dumps([m.text for m in p.manual])


def test_built_tgz_is_manual(tmp_path):
    listing = PackageListing(
        "my-app-1.0.0.tgz",
        ["package/.claude/settings.local.json"],
        {"package/.claude/settings.local.json": b"{}"},
    )
    p = fix.plan(check_listing(listing), pack=True)
    assert not p.changes
    assert any(m.check_id == "AP006" for m in p.manual)


def test_created_npmignore_undone_only_if_untouched(tmp_path):
    root = tmp_path / "my-app"
    root.mkdir()
    (root / "package.json").write_text('{"name": "my-app", "version": "1.0.0"}')
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text("{}")
    (root / ".gitignore").write_text("dist/\n")
    listing = PackageListing(
        "my-app-1.0.0.tgz",
        [".claude/settings.local.json"],
        {".claude/settings.local.json": b"{}"},
    )
    home = tmp_path / "home"
    p = fix.plan(check_listing(listing), pack=True, root=root)
    fix.apply(p, home)
    assert (root / ".npmignore").read_text() == "dist/\n/.claude/settings.local.json\n"
    restored, cannot = fix.undo(home)
    assert not (root / ".npmignore").exists()
    assert (root / ".gitignore").read_text() == "dist/\n"
    assert restored


# ---- the ignore line names the file, never a shared folder (review C1) -------------------------


@pytest.mark.parametrize(
    ("member", "line"),
    [
        (".claude/settings.local.json", "/.claude/settings.local.json"),
        (".claude/projects/x/s1.jsonl", "/.claude/projects/x/s1.jsonl"),
        ("sub/.claude/settings.local.json", "/sub/.claude/settings.local.json"),
        (".github/copilot/settings.local.json", "/.github/copilot/settings.local.json"),
        (".github/mcp.json", "/.github/mcp.json"),
        (".agents/mcp_config.json", "/.agents/mcp_config.json"),
        (".cursor/mcp.json", "/.cursor/mcp.json"),
        (".gemini/oauth_creds.json", "/.gemini/oauth_creds.json"),
        (".copilot/session-store.db", "/.copilot/session-store.db"),
        # A rule about a whole folder of agent data: that folder, and nothing above it.
        (".codex/sessions/2026/11/r.jsonl", "/.codex/sessions/"),
        (".cursor/projects/p/agent-transcripts/t.jsonl", "/.cursor/projects/"),
        (".gemini/tmp/abc/logs.json", "/.gemini/tmp/"),
        (".gemini/antigravity/brain/c1/task.md", "/.gemini/antigravity/"),
        (".copilot/logs/a.log", "/.copilot/logs/"),
        ("pkg/.hermes/sessions/s.json", "/pkg/.hermes/"),
        ("weird [name]/.codex/sessions/r.jsonl", "/weird \\[name\\]/.codex/sessions/"),
    ],
)
def test_package_line_names_the_file_or_the_folder_its_rule_names(member, line):
    assert fix.package_line(member) == line
    assert line.strip("/") not in fix.SHARED_FOLDERS


def test_package_line_refuses_a_name_that_is_not_one_line():
    assert fix.package_line(".claude/settings.local.json\n.claude") is None


def test_a_shared_folder_is_never_planned_as_an_ignore_line(tmp_path):
    root = tmp_path / "my-app"
    root.mkdir()
    (root / "package.json").write_text('{"name": "my-app", "version": "1.0.0"}')
    members = [".claude/settings.local.json", ".github/copilot/settings.local.json"]
    for member in members:
        (root / member).parent.mkdir(parents=True, exist_ok=True)
        (root / member).write_text("{}")
    listing = PackageListing("my-app-1.0.0.tgz", members, {m: b"{}" for m in members})
    p = fix.plan(check_listing(listing), pack=True, root=root)
    lines = {(c.path.name, c.line_text) for c in p.changes}
    assert lines == {
        (name, f"/{member}") for name in (".npmignore", ".gitignore") for member in members
    }


def test_ap009_goes_to_needs_you_with_person_wording(tmp_path):
    from agentprint.checks import ap009_source_exposure as ap009

    listing = PackageListing(
        "demo-1.0.0.tgz",
        ["package/package.json", "package/dist/a.js.map"],
        {
            "package/package.json": b'{"name": "demo", "version": "1.0.0"}',
            "package/dist/a.js.map": b'{"version":3,"mappings":"","sources":["a.ts"],'
            b'"sourcesContent":["x"]}',
        },
    )
    findings = ap009.check_listing(listing)
    p = fix.plan(findings, pack=True, root=tmp_path)
    assert not p.changes
    [item] = [m for m in p.manual if m.check_id == "AP009"]
    assert "The agent cannot do this" not in item.person
    assert "devtool" in item.person and "sourceMap" in item.person
    assert "open source" in item.person
