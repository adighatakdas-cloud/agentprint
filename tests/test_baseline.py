"""The baseline and ignore rules: known findings, the fingerprint, and Ruling C1.

Ruling C1: a key that would ship in a package being published, or in a push, always blocks,
whatever the config or baseline says; ignore and baseline still apply to the other findings.
"""

import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint import baseline as bl
from agentprint import guard, hook
from agentprint.baseline import Entry, Project, fingerprint, settle
from agentprint.cli import app
from agentprint.config import Config
from agentprint.guard import evaluate
from agentprint.models import Finding, Severity
from tests import fakes
from tests.test_hooks import blocked, payload
from tests.test_mcp import call

runner = CliRunner()
SETTINGS = ".claude/settings.json"
LEAKY = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}}).encode()


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch):
    monkeypatch.setattr(bl, "today", lambda: "2026-11-07")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def project(tmp_path):
    """A key (AP002) and Bash(*) (AP003) in shared Claude Code settings."""
    root = tmp_path / "proj"
    allow = [f"Bash(export ANTHROPIC_API_KEY={fakes.ANTHROPIC})", "Bash(*)"]
    _write(root / SETTINGS, json.dumps({"permissions": {"allow": allow}}))
    return root


def scan(root, *args):
    return runner.invoke(app, ["scan", str(root), "--no-home", *args])


def baseline(root, *args):
    return runner.invoke(app, ["baseline", str(root), *args])


def _tgz(path, members):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def _windows(key: str, size: int = 5):
    return {key[i : i + size] for i in range(len(key) - size + 1)}


# ---- the round trip ---------------------------------------------------------------------------


def test_baseline_turns_todays_findings_into_known(project):
    assert scan(project, "--format", "agent").exit_code == 1
    written = baseline(project, "--format", "agent")
    assert written.exit_code == 0, written.output
    assert written.output.startswith("agentprint baseline: wrote ")
    assert "ask_human:" in written.output and "rotate" in written.output  # one is a key

    agent = scan(project, "--format", "agent")
    assert agent.exit_code == 0
    first, known, *notes = agent.output.strip().splitlines()
    assert first == "agentprint: OK"
    assert all(note.startswith("not_checked: ") for note in notes)  # Windows: the AP008 note
    assert known.startswith("known: 2 accepted by this project, not counted")
    assert 'AP002 in the baseline "accepted 2026-11-07"' in known

    data = json.loads(scan(project, "--format", "json").stdout)
    assert data["findings"] == [] and data["summary"]["known"] == 2
    assert {(k["check_id"], k["path"], k["via"]) for k in data["known"]} == {
        ("AP002", SETTINGS, "baseline"),
        ("AP003", SETTINGS, "baseline"),
    }

    human = scan(project, "--format", "table", "--plain")
    assert human.exit_code == 0
    assert "known     2" in human.output and "2 known · keys masked" in human.output
    assert "CLEAR" in human.output

    sarif = json.loads(scan(project, "--format", "sarif").stdout)
    assert sarif["runs"][0]["results"] == []  # only what counts goes to code scanning


def test_the_baseline_file_is_sorted_stable_and_names_only_check_path_reason(project, tmp_path):
    _write(project / ".mcp.json", json.dumps({"mcpServers": {"x": {"env": {"K": fakes.GITHUB}}}}))
    assert baseline(project).exit_code == 0
    file = project / ".agentprint-baseline.json"
    once = file.read_bytes()
    assert baseline(project).exit_code == 0
    assert file.read_bytes() == once  # same findings, same bytes
    data = json.loads(once)
    assert data["tool"] == "agentprint" and data["format"] == 1
    rows = data["entries"]
    assert [(r["path"], r["check"]) for r in rows] == sorted((r["path"], r["check"]) for r in rows)
    assert all(set(r) == {"fingerprint", "check", "path", "reason", "count"} for r in rows)
    assert {r["reason"] for r in rows} == {"accepted 2026-11-07"}


def test_the_baseline_file_holds_no_key_bytes(project, fake_home):
    """Not the key, not its stub, not any 5 characters of it (redact() shows 4)."""
    result = baseline(project, "--include-home", "--home", str(fake_home))
    assert result.exit_code == 0, result.output
    text = (project / ".agentprint-baseline.json").read_text(encoding="utf-8")
    assert '"~/.claude/history.jsonl"' in text  # home findings are in it this time
    assert "…(len" not in text  # not even the masked stub
    for key in fakes.ALL.values():
        leaked = [part for part in _windows(key) if part in text]
        assert not leaked, (key[:4], leaked)
    assert not any(part in result.output for part in _windows(fakes.ANTHROPIC))


def test_the_fingerprint_is_the_documented_hash_of_redacted_parts():
    key = Finding(
        "AP002", Severity.CRITICAL, "t", "a", "x", 3, "Anthropic API key: sk-a…(len 9)", "f"
    )
    expected = "AP002|.claude/settings.json|Anthropic API key|sk-a…(len 9)"
    assert fingerprint(key, SETTINGS) == hashlib.sha256(expected.encode()).hexdigest()
    hidden = Finding(
        "AP001", Severity.HIGH, "t", "a", "x", None, "Rule: ghp_…(len 40) (hidden: base64)", "f"
    )
    assert (
        fingerprint(hidden, "h")
        == hashlib.sha256(b"AP001|h|Rule|ghp_\xe2\x80\xa6(len 40)").hexdigest()
    )
    grant = Finding("AP003", Severity.MEDIUM, "Over-broad agent permission", "a", "x", 7, "d", "f")
    expected = "AP003|.claude/settings.json|Over-broad agent permission|"
    assert fingerprint(grant, SETTINGS) == hashlib.sha256(expected.encode()).hexdigest()
    moved = Finding("AP003", Severity.MEDIUM, grant.title, "a", "x", 90, "other words", "f")
    assert fingerprint(moved, SETTINGS) == fingerprint(grant, SETTINGS)  # lines move


def test_findings_stay_known_when_their_lines_move(project):
    assert baseline(project).exit_code == 0
    settings = project / SETTINGS
    data = json.loads(settings.read_text())
    data["model"] = "x"
    settings.write_text("\n\n\n" + json.dumps(data, indent=4) + "\n")
    assert scan(project, "--format", "agent").output.startswith("agentprint: OK")


def test_a_second_finding_with_the_same_fingerprint_is_new():
    detail = "Anthropic API key: sk-a…(len 105)"
    old = Finding("AP001", Severity.HIGH, "t", "claude-code", "/p/h.jsonl", 1, detail, "f")
    new = Finding("AP001", Severity.HIGH, "t", "claude-code", "/p/h.jsonl", 9, detail, "f")
    fp = fingerprint(old, "h.jsonl")
    entry = Entry(fp, "AP001", "h.jsonl", "accepted 2026-11-07", count=1)
    settled = settle([old, new], Project(Config(Path("/p")), {fp: entry}), None, publishing=False)
    assert settled.active == [new] and [k.finding for k in settled.known] == [old]


def test_check_lists_stale_entries_and_prune_drops_them(project):
    assert baseline(project).exit_code == 0
    file = project / ".agentprint-baseline.json"
    before = file.read_bytes()
    _write(project / SETTINGS, json.dumps({"permissions": {"allow": ["Bash(*)"]}}))  # key gone
    hook_cmd = {"hooks": [{"type": "command", "command": "curl https://x.example.invalid"}]}
    hooks = {"hooks": {"Stop": [hook_cmd]}, "permissions": {"allow": ["Bash(*)"]}}
    _write(project / SETTINGS, json.dumps(hooks))  # and a new AP004

    checked = baseline(project, "--check", "--format", "json")
    assert checked.exit_code == 0 and file.read_bytes() == before  # --check changes nothing
    data = json.loads(checked.stdout)
    assert (data["entries"], data["matched"], data["new"]) == (2, 1, 1)
    assert [(s["check_id"], s["path"]) for s in data["stale"]] == [("AP002", SETTINGS)]
    agent = baseline(project, "--check", "--format", "agent").output
    assert "1 stale" in agent and "agentprint baseline --prune" in agent

    updated = json.loads(baseline(project, "--update", "--format", "json").stdout)
    assert (updated["entries"], updated["added"], updated["dropped"]) == (3, 1, 0)  # keeps old
    pruned = json.loads(baseline(project, "--prune", "--format", "json").stdout)
    assert (pruned["entries"], pruned["dropped"]) == (2, 1)
    assert json.loads(baseline(project, "--check", "--format", "json").stdout)["stale"] == []


def test_check_for_a_person(project):
    assert baseline(project).exit_code == 0
    assert "2 entries · 2 still found · 0 stale · 0 new" in baseline(project, "--check").output
    _write(project / SETTINGS, "{}")
    shown = baseline(project, "--check", "--plain").output
    assert "0 still found · 2 stale" in shown and "AP002  .claude/settings.json" in shown
    assert "agentprint baseline --prune drops the stale entries." in shown
    empty = baseline(project, "--check", "--baseline", str(project / "none.json"), "-f", "agent")
    assert empty.output.startswith("agentprint baseline --check: no entries in ")


def test_baseline_errors_exit_2(project, tmp_path):
    assert baseline(tmp_path / "missing").exit_code == 2
    assert baseline(project, "--include-home", "--home", str(tmp_path / "nohome")).exit_code == 2
    nowhere = tmp_path / "no" / "such" / "folder" / "b.json"
    result = baseline(project, "--baseline", str(nowhere))
    assert result.exit_code == 2 and "cannot write the baseline" in result.output


def test_check_names_new_findings_for_a_person(project):
    _write(project / ".agentprint-baseline.json", json.dumps({"entries": []}))
    shown = baseline(project, "--check", "--plain").output
    assert "0 entries · 0 still found · 0 stale · 2 new" in shown
    assert "agentprint scan shows the new ones." in shown


def test_rewriting_keeps_the_reasons_of_entries_still_found(project, monkeypatch):
    assert baseline(project).exit_code == 0
    monkeypatch.setattr(bl, "today", lambda: "2027-01-01")
    _write(project / ".mcp.json", json.dumps({"mcpServers": {"x": {"env": {"K": fakes.GITHUB}}}}))
    assert baseline(project).exit_code == 0
    rows = json.loads((project / ".agentprint-baseline.json").read_text())["entries"]
    reasons = {(r["path"], r["check"]): r["reason"] for r in rows}
    assert reasons[(SETTINGS, "AP003")] == "accepted 2026-11-07"
    assert reasons[(".mcp.json", "AP005")] == "accepted 2027-01-01"


def test_home_findings_stay_out_of_the_baseline_unless_asked(project, fake_home):
    assert baseline(project).exit_code == 0
    text = (project / ".agentprint-baseline.json").read_text()
    assert '"~' not in text
    with_home = runner.invoke(app, ["scan", str(project), "--home", str(fake_home), "-f", "json"])
    assert any(f["check_id"] == "AP001" for f in json.loads(with_home.stdout)["findings"])

    assert baseline(project, "--include-home", "--home", str(fake_home)).exit_code == 0
    swept = runner.invoke(app, ["scan", str(project), "--home", str(fake_home), "-f", "json"])
    assert swept.exit_code == 0 and json.loads(swept.stdout)["findings"] == []


def test_ignore_rules_never_hide_home_findings(project, fake_home):
    _write(project / ".agentprint.toml", '[[ignore]]\ncheck = "AP001"\nreason = "not ours"\n')
    swept = runner.invoke(app, ["scan", str(project), "--home", str(fake_home), "-f", "json"])
    data = json.loads(swept.stdout)
    assert any(f["check_id"] == "AP001" for f in data["findings"])  # a repo can't silence ~
    assert "known" not in data


def test_ignore_rules_by_check_and_glob(project):
    rules = '[[ignore]]\ncheck = "AP003"\npath = ".claude/*.json"\nreason = "devcontainer"\n'
    _write(project / ".agentprint.toml", rules)
    data = json.loads(scan(project, "-f", "json").stdout)
    assert [f["check_id"] for f in data["findings"]] == ["AP002"]
    assert data["known"] == [
        {
            "check_id": "AP003",
            "severity": "medium",
            "path": SETTINGS,
            "via": "ignore",
            "reason": "devcontainer",
        }
    ]
    _write(project / ".agentprint.toml", '[[ignore]]\ncheck = "AP003"\npath = "other/**"\n')
    assert "known" not in json.loads(scan(project, "-f", "json").stdout)


def test_no_config_counts_everything_again(project):
    assert baseline(project).exit_code == 0
    assert scan(project, "--no-config").exit_code == 1
    assert baseline(project, "--no-config").exit_code == 2  # no file to write to
    other = project / "elsewhere.json"
    assert scan(project, "--baseline", str(other)).exit_code == 1  # an empty baseline


@pytest.mark.posix_only
def test_the_baseline_is_never_written_through_a_link_or_outside_the_project(project, tmp_path):
    target = _write(tmp_path / "dotfile", "keep me\n")
    (project / ".agentprint-baseline.json").symlink_to(target)
    result = baseline(project)
    assert result.exit_code == 2 and "symbolic link" in result.output
    assert target.read_text() == "keep me\n"
    (project / ".agentprint-baseline.json").unlink()
    (tmp_path / "out").mkdir()
    (project / "ci").symlink_to(tmp_path / "out")
    _write(project / ".agentprint.toml", 'baseline = "ci/b.json"\n')
    result = baseline(project)
    assert result.exit_code == 2 and "outside the project" in result.output
    assert not (tmp_path / "out" / "b.json").exists()
    chosen = tmp_path / "out" / "mine.json"
    assert baseline(project, "--baseline", str(chosen)).exit_code == 0  # your own choice
    assert chosen.is_file()


def test_only_one_baseline_mode_at_a_time(project):
    result = baseline(project, "--update", "--check")
    assert result.exit_code == 2 and "pick one" in result.output


def test_a_broken_baseline_warns_and_nothing_is_known(project):
    _write(project / ".agentprint-baseline.json", "{not json")
    result = scan(project, "--format", "json")
    assert result.exit_code == 1
    assert "could not read baseline" in result.stderr
    assert json.loads(result.stdout)["findings"]  # stdout stays valid JSON
    rows = [{"fingerprint": "x" * 64}, {"fingerprint": "0" * 64, "count": 0}, "nope"]
    _write(project / ".agentprint-baseline.json", json.dumps({"entries": rows}))
    assert "3 entries in baseline" in scan(project).stderr


def test_the_badge_verdict_counts_known_findings(project, tmp_path):
    assert baseline(project).exit_code == 0
    out = tmp_path / "badge"
    out.mkdir()
    assert scan(project, "--badge", str(out)).exit_code == 0
    verdict = json.loads((out / "verdict.json").read_text())
    assert verdict["verdict"] == "CLEAR" and verdict["summary"]["known"] == 2


def test_key_checks_are_exactly_the_findings_that_hold_a_masked_key(
    fake_home, agents_home, fake_project
):
    """Ruling C1 leans on KEY_CHECKS: a check that reports a key must be in it."""
    from agentprint.scanner import scan as run_scan

    shape = re.compile(r"[^:\n]+: \S{1,4}…\(len \d+\)")
    findings = run_scan(fake_home, fake_project) + run_scan(agents_home, None)
    findings += guard.check_package_path(
        _tgz(fake_project.parent / "k.tgz", {"package/.claude/settings.local.json": LEAKY})
    )
    keys = [f for f in findings if shape.match(f.detail)]
    assert {f.check_id for f in keys} == {"AP001", "AP002", "AP005"} == set(bl.KEY_CHECKS)
    assert all(shape.match(f.detail) for f in findings if bl.is_key(f))


def test_baseline_files_that_cannot_be_used_warn(tmp_path, monkeypatch):
    assert bl.read(tmp_path / "missing.json") == ({}, [])  # no file: nothing, quietly
    assert "could not read baseline" in bl.read(tmp_path)[1][0]  # a folder
    for text in ("[]", '{"entries": 3}'):
        assert bl.read(_write(tmp_path / "b.json", text))[1] == [
            f"could not read baseline {tmp_path / 'b.json'}: no entries list"
        ]
    monkeypatch.setattr(bl, "MAX_BASELINE_BYTES", 10)
    assert "larger than" in bl.read(_write(tmp_path / "b.json", '{"entries": []}'))[1][0]


def test_where_names_project_home_package_and_other_files(tmp_path):
    def at(location):
        return Finding("AP003", Severity.MEDIUM, "t", "a", location, None, "d", "f")

    root, home = tmp_path / "proj", tmp_path / "home"
    assert bl.where(at(str(root / "a" / "b.json")), root, home) == ("a/b.json", True)
    assert bl.where(at(str(home / ".claude" / "x")), root, home) == ("~/.claude/x", False)
    assert bl.where(at(str(home)), root, home) == ("~", False)
    elsewhere = tmp_path / "other" / "x.json"
    assert bl.where(at(str(elsewhere)), root, home) == (elsewhere.absolute().as_posix(), False)
    member = at("demo-1.0.0.tgz!package/.claude/settings.local.json")
    assert bl.where(member, root, home, package=True) == (
        "package/.claude/settings.local.json",
        True,
    )
    assert bl.where(member, root, home, package=None)[0] == "package/.claude/settings.local.json"


def test_fix_and_pack_show_known_findings_to_a_person(project, tmp_path):
    assert baseline(project).exit_code == 0
    fixed = runner.invoke(app, ["fix", str(project), "--no-home", "--dry-run", "-f", "table"])
    assert fixed.exit_code == 0
    assert "known     2" in fixed.output and "2 known · keys masked" in fixed.output
    tgz = _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": b"{}"})
    rule = '[[ignore]]\ncheck = "AP006"\npath = "**/settings.local.json"\nreason = "demo"\n'
    _write(project / ".agentprint.toml", rule)
    packed = runner.invoke(
        app, ["pack", str(tgz), "--config", str(project / ".agentprint.toml"), "-f", "table"]
    )
    assert packed.exit_code == 0, packed.output
    assert "AP006  1 ignored by config · demo" in packed.output


# ---- the config on the command line -----------------------------------------------------------


def test_config_fail_on_applies_and_the_command_line_wins(tmp_path):
    root = tmp_path / "p"
    _write(root / SETTINGS, json.dumps({"permissions": {"allow": ["Bash(*)"]}}))  # medium
    assert scan(root).exit_code == 0
    _write(root / ".agentprint.toml", 'fail_on = "medium"\n')
    assert scan(root).exit_code == 1
    assert scan(root, "--fail-on", "high").exit_code == 0


def test_config_problems_are_warnings_on_stderr(project):
    _write(project / ".agentprint.toml", "fail_on = [\n")
    result = scan(project, "--format", "json")
    assert result.exit_code == 1
    assert result.stderr.startswith("warning: could not read config ")
    assert "using the defaults" in result.stderr
    json.loads(result.stdout)


def test_fix_leaves_known_findings_alone(project):
    assert baseline(project).exit_code == 0
    result = runner.invoke(
        app, ["fix", str(project), "--no-home", "--dry-run", "--format", "agent"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith("agentprint fix: CLEAR — nothing to fix")
    assert "\nknown: 2 accepted by this project" in result.output
    data = json.loads(
        runner.invoke(app, ["fix", str(project), "--no-home", "--dry-run", "-f", "json"]).stdout
    )
    assert data["planned"] == [] and data["after"]["summary"]["known"] == 2


# ---- Ruling C1: a key on its way out always blocks --------------------------------------------

EVERYTHING_ALLOWED = """
fail_on = "none"
[[ignore]]
check = "AP002"
reason = "trust me"
[[ignore]]
check = "AP006"
reason = "we ship it on purpose"
"""


def _accept_everything(root, findings):
    """A baseline that covers every finding (as a hostile or careless one could)."""
    rows = []
    for f in findings:
        path, _ = bl.where(f, root, None, package=None)
        fp = fingerprint(f, path)
        rows.append({"fingerprint": fp, "check": f.check_id, "path": path, "reason": "ok"})
    _write(root / ".agentprint-baseline.json", json.dumps({"entries": rows}))


def test_ruling_c1_a_key_in_a_publish_blocks_whatever_the_config_says(tmp_path):
    tgz = _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": LEAKY})
    _write(tmp_path / ".agentprint.toml", EVERYTHING_ALLOWED)
    _accept_everything(tmp_path, guard.check_package_path(tgz))

    verdict = evaluate("npm publish demo-1.0.0.tgz", tmp_path)
    assert verdict is not None and verdict.block
    assert verdict.reason.startswith("agentprint: BLOCK — 1 problem to fix before publishing")
    assert "AP002" in verdict.reason and "[2]" not in verdict.reason  # AP006 is known
    assert '1 AP006 ignored by config "we ship it on purpose"' in verdict.reason
    assert fakes.ANTHROPIC not in verdict.reason

    reply = hook.run_hook(
        "claude-code",
        json.dumps(payload("claude-code", "npm publish demo-1.0.0.tgz", tmp_path)),
        {},
    )
    assert blocked("claude-code", reply).startswith("agentprint: BLOCK")


def test_ruling_c1_in_pack_and_the_mcp_tool(tmp_path, monkeypatch):
    tgz = _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": LEAKY})
    _write(tmp_path / ".agentprint.toml", EVERYTHING_ALLOWED)
    monkeypatch.chdir(tmp_path)  # a built file: the config comes from the current folder
    result = runner.invoke(app, ["pack", str(tgz), "--format", "agent"])
    assert result.exit_code == 1 and result.output.startswith("agentprint: BLOCK — 1 problem")
    assert call("check_package", path=str(tgz)).startswith("agentprint: BLOCK — 1 problem")
    # --fail-on typed on the command line is the person's own choice, as before.
    assert runner.invoke(app, ["pack", str(tgz), "--fail-on", "none"]).exit_code == 0


def test_ruling_c1_a_key_in_a_push_blocks_and_other_findings_can_be_known(tmp_path, monkeypatch):
    key = Finding(
        "AP005",
        Severity.CRITICAL,
        "Secret inline in MCP server config",
        "claude-code",
        str(tmp_path / ".mcp.json"),
        3,
        "GitHub token: ghp_…(len 40) in MCP server 'x' (env.K)",
        "f",
    )
    personal = Finding(
        "AP007", Severity.LOW, "t", "agent", str(tmp_path / "CLAUDE.local.md"), None, "d", "f"
    )
    monkeypatch.setattr(guard, "check_push", lambda cwd: [key, personal])
    rules = '[[ignore]]\ncheck = "AP005"\n[[ignore]]\npath = "CLAUDE.local.md"\nreason = "fine"\n'
    _write(tmp_path / ".agentprint.toml", 'fail_on = "none"\n' + rules)
    verdict = evaluate("git push", tmp_path)
    assert verdict is not None and verdict.block
    assert verdict.reason.startswith("agentprint: BLOCK — 1 problem to fix before pushing")
    assert '1 AP007 ignored by config "fine"' in verdict.reason


def test_ignore_still_lets_a_deliberate_non_secret_file_ship(tmp_path):
    _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": b'{"x": 1}'})
    assert evaluate("npm publish demo-1.0.0.tgz", tmp_path).block  # AP006, high
    rule = '[[ignore]]\ncheck = "AP006"\npath = "**/.claude/settings.local.json"\nreason = "demo"\n'
    _write(tmp_path / ".agentprint.toml", rule)
    verdict = evaluate("npm publish demo-1.0.0.tgz", tmp_path)
    assert verdict is not None and not verdict.block and verdict.warning is None


def test_config_strict_makes_the_guard_strict(tmp_path):
    assert not evaluate("poetry publish --build", tmp_path).block  # nothing built: allowed
    _write(tmp_path / ".agentprint.toml", "strict = true\n")
    verdict = evaluate("poetry publish --build", tmp_path)
    assert verdict.block and "strict mode" in verdict.reason
    _write(tmp_path / ".agentprint.toml", "strict = false\n")  # can't turn the flag off
    assert evaluate("poetry publish --build", tmp_path, strict=True).block


def test_config_strict_holds_when_the_command_parser_fails(tmp_path, monkeypatch):
    def boom(command, cwd):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(guard, "detect", boom)
    assert not evaluate("npm publish", tmp_path).block  # fail open by default
    _write(tmp_path / ".agentprint.toml", "strict = true\n")
    verdict = evaluate("npm publish", tmp_path)
    assert verdict.block and "strict mode" in verdict.reason


def test_config_warnings_reach_the_agent_but_never_trip_strict_mode(tmp_path):
    _tgz(tmp_path / "ok-1.0.0.tgz", {"package/index.js": b"1"})
    _write(tmp_path / ".agentprint.toml", "colour = 1\n")
    verdict = evaluate("npm publish ok-1.0.0.tgz", tmp_path, strict=True)
    assert verdict is not None and not verdict.block
    assert "unknown keys" in verdict.warning


def test_a_config_bug_never_stops_the_guard(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "load_project", boom)
    _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": LEAKY})
    assert evaluate("npm publish demo-1.0.0.tgz", tmp_path).block  # the defaults: all counts


def test_mcp_scan_honours_the_project_config(project):
    assert baseline(project).exit_code == 0
    text = call("scan_secrets", path=str(project))
    assert text.startswith("agentprint: OK\nknown: 2 accepted by this project")
    _write(project / ".agentprint.toml", "colour = 1\n")
    assert "\nwarning: unknown keys" in call("scan_secrets", path=str(project))
