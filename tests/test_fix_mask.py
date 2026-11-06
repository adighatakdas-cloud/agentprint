"""agentprint fix, part 1: the plan, and masking keys in place (AP001, AP002 allowlist entries).

Every home is a temporary folder; every key comes from tests.fakes (or is an obviously fake
generic value). Masked files must keep every byte that was not part of a key.
"""

import dataclasses
import json
import re
import sqlite3
import stat
from pathlib import Path

import pytest

from agentprint import fix
from agentprint.scanner import scan
from agentprint.secrets import redact
from tests import fakes


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _history(tmp_path: Path, data: bytes) -> tuple[Path, Path]:
    home = tmp_path / "home"
    return home, _write(home / ".claude" / "history.jsonl", data)


def _line(text: str) -> bytes:
    return (json.dumps({"display": text}) + "\n").encode()


def _masked(data: bytes, *keys: str) -> bytes:
    for key in keys:
        data = data.replace(key.encode(), fix.mask_text(key).encode())
    return data


def _fix(home: Path) -> tuple[fix.Plan, fix.Outcome]:
    p = fix.plan(scan(home, None))
    return p, fix.apply(p, home)


def _ap001(home: Path) -> list:
    return [f for f in scan(home, None) if f.check_id == "AP001"]


def test_mask_text_keeps_prefix_and_length():
    value = "sk-ant-FAKE" + "x" * 40
    masked = fix.mask_text(value)
    assert masked.startswith("sk-a")
    assert len(masked) == len(value)
    assert masked[4:] == "*" * (len(value) - 4)


def test_history_key_masked_in_place(tmp_path):
    original = _line("hello") + _line(f"my key is {fakes.ANTHROPIC}") + _line("bye")
    home, path = _history(tmp_path, original)
    path.chmod(0o640)
    before = path.stat()
    p, outcome = _fix(home)
    [change] = p.changes
    assert (change.kind, change.check_id, change.path, change.line) == ("mask", "AP001", path, 2)
    assert change.shown == redact(fakes.ANTHROPIC)
    assert outcome.applied == [change] and outcome.skipped == []
    assert path.read_bytes() == _masked(original, fakes.ANTHROPIC)
    after = path.stat()
    assert (after.st_ino, after.st_dev) == (before.st_ino, before.st_dev)
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert _ap001(home) == []
    # The key is gone from the file, but it was exposed: rotating it is left to the human.
    assert any(m.check_id == "AP001" and "Rotate" in m.text for m in p.manual)
    assert fakes.ANTHROPIC not in repr(p)


def test_every_occurrence_masked(tmp_path):
    original = (
        _line(f"first {fakes.ANTHROPIC}")
        + _line(f"again {fakes.ANTHROPIC} and {fakes.ANTHROPIC}")
        + _line(f"two keys {fakes.GITHUB} {fakes.AWS}")
    )
    home, path = _history(tmp_path, original)
    p, outcome = _fix(home)
    assert outcome.skipped == []
    data = path.read_bytes()
    assert data == _masked(original, fakes.ANTHROPIC, fakes.GITHUB, fakes.AWS)
    assert len(data) == len(original)
    by_key = {c.shown: c for c in p.changes}
    assert len(by_key[redact(fakes.ANTHROPIC)].offsets) == 3
    assert by_key[redact(fakes.GITHUB)].line == by_key[redact(fakes.AWS)].line == 3
    assert _ap001(home) == []


def test_appends_during_fix_survive(tmp_path):
    original = _line(f"key {fakes.OPENAI}")
    home, path = _history(tmp_path, original)
    p = fix.plan(scan(home, None))
    appended = _line("the agent kept talking while fix ran")
    with path.open("ab") as fh:
        fh.write(appended)
    outcome = fix.apply(p, home)
    assert outcome.skipped == []
    assert path.read_bytes() == _masked(original, fakes.OPENAI) + appended


def test_changed_bytes_are_skipped(tmp_path):
    original = _line("hi") + _line(f"key {fakes.GITHUB}")
    home, path = _history(tmp_path, original)
    p = fix.plan(scan(home, None))
    changed = original.replace(fakes.GITHUB.encode(), b"x" * len(fakes.GITHUB))
    path.write_bytes(changed)
    outcome = fix.apply(p, home)
    assert outcome.applied == []
    [(change, reason)] = outcome.skipped
    assert change.path == path
    assert "changed" in reason and "agentprint fix" in reason
    assert path.read_bytes() == changed
    assert outcome.journal is None


def test_crlf_and_invalid_utf8_preserved(tmp_path):
    original = (
        b"caf\xe9 \xff\xfe " + fakes.STRIPE.encode() + b" tail\r\n"
        b"plain line\r\n"
        b"\x80\x81 \xc3( " + fakes.SLACK.encode() + b"\r\n"
        b"no newline at the end \xed\xa0\x80"
    )
    home, path = _history(tmp_path, original)
    p, outcome = _fix(home)
    assert len(p.changes) == 2 and outcome.skipped == []
    assert path.read_bytes() == _masked(original, fakes.STRIPE, fakes.SLACK)
    assert _ap001(home) == []


def test_sqlite_history_is_manual(tmp_path):
    home = tmp_path / "home"
    db = home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
    db = db / "state.vscdb"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE "cursorDiskKV" (key TEXT, value BLOB)')
    bubble = json.dumps({"type": 1, "text": f"my key {fakes.ANTHROPIC}"}).encode()
    con.execute('INSERT INTO "cursorDiskKV" VALUES (?, ?)', ("bubbleId:c:b", bubble))
    con.commit()
    con.close()
    before = db.read_bytes()
    [finding] = _ap001(home)
    assert finding.line is None  # database rows have no line
    for reported in (finding, dataclasses.replace(finding, line=0)):
        p = fix.plan([reported])
        assert p.changes == []
        [item] = p.manual
        assert item.check_id == "AP001"
        assert "chat" in item.text and "Rotate" in item.text
        assert fakes.ANTHROPIC not in item.text
    outcome = fix.apply(p, home)
    assert outcome.applied == [] and outcome.journal is None
    assert db.read_bytes() == before


def test_allowlist_key_masked_config_value_left(tmp_path):
    home = tmp_path / "home"
    settings = {
        "permissions": {
            "allow": [f"Bash(git push https://x:{fakes.GITHUB}@github.com/o/r)", "Bash(ls)"]
        },
        "env": {"OPENAI_API_KEY": fakes.OPENAI},
    }
    original = json.dumps(settings, indent=2).encode()
    path = _write(home / ".claude" / "settings.json", original)
    p, outcome = _fix(home)
    [change] = p.changes
    assert (change.check_id, change.path, change.shown) == ("AP002", path, redact(fakes.GITHUB))
    assert outcome.applied == [change]
    data = path.read_bytes()
    assert data == _masked(original, fakes.GITHUB)  # the config value's bytes are unchanged
    assert json.loads(data)["env"] == settings["env"]
    manual = [m.text for m in p.manual if m.check_id == "AP002"]
    assert any("environment variable" in text for text in manual)  # the config value
    assert any("Rotate the GitHub token" in text for text in manual)  # the masked key


def test_same_key_in_allowlist_and_config_masks_only_the_allowlist(tmp_path):
    home = tmp_path / "home"
    settings = {
        "permissions": {"allow": [f"Bash(export OPENAI_API_KEY={fakes.OPENAI})"]},
        "env": {"OPENAI_API_KEY": fakes.OPENAI},
    }
    original = json.dumps(settings, indent=2).encode()
    path = _write(home / ".claude" / "settings.json", original)
    p, outcome = _fix(home)
    [change] = p.changes
    assert len(change.offsets) == 1 and outcome.skipped == []
    key, mask = fakes.OPENAI.encode(), fix.mask_text(fakes.OPENAI).encode()
    assert path.read_bytes() == original.replace(key, mask, 1)
    assert json.loads(path.read_bytes())["env"] == settings["env"]
    assert any("environment variable" in m.text for m in p.manual if m.check_id == "AP002")


def test_mask_never_breaks_a_json_line(tmp_path):
    # The generic rule reads "\u0041bc..." as a value starting "u0041": keeping its first four
    # characters would cut the JSON escape in half, so this one is left to the human.
    original = b'{"display": "password=\\u0041bcdefghij1234567890KLMNOPqrs"}\n'
    home, path = _history(tmp_path, original)
    findings = _ap001(home)
    assert len(findings) == 1
    p = fix.plan(findings)
    assert p.changes == []
    assert [m.check_id for m in p.manual] == ["AP001"]
    assert fix.apply(p, home).applied == []
    assert path.read_bytes() == original


def test_other_prints_are_left_to_the_human(tmp_path):
    home = tmp_path / "home"
    settings = {"permissions": {"allow": ["Bash(*)"]}}
    path = _write(home / ".claude" / "settings.json", json.dumps(settings).encode())
    findings = [f for f in scan(home, None) if f.check_id == "AP003"]
    assert findings
    p = fix.plan(findings)
    assert p.changes == [] and p.findings == findings
    assert [m.check_id for m in p.manual] == ["AP003"]
    assert str(path) in p.manual[0].text


def test_journal_has_no_key(tmp_path):
    original = _line(f"key {fakes.ANTHROPIC}")
    home, path = _history(tmp_path, original)
    _, outcome = _fix(home)
    journal = outcome.journal
    assert journal is not None and journal.parent == home / ".agentprint" / "fix"
    assert re.fullmatch(r"\d{8}T\d{6}Z\.json", journal.name)
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.parent.stat().st_mode) == 0o700
    text = journal.read_text()
    assert fakes.ANTHROPIC not in text
    assert fix.mask_text(fakes.ANTHROPIC) not in text
    [entry] = json.loads(text)["changes"]
    assert entry["kind"] == "mask" and entry["path"] == str(path) and entry["line"] == 1
    assert entry["shown"] == redact(fakes.ANTHROPIC)


_VSCODE = Path("Library") / "Application Support"
ALLOWLISTS = {
    "gemini-cli": (
        Path(".gemini") / "settings.json",
        json.dumps({"tools": {"allowed": [f"run_shell_command(export K={fakes.OPENAI})"]}}),
    ),
    "windsurf": (
        _VSCODE / "Windsurf" / "User" / "settings.json",
        '{\n  // Windsurf\n  "windsurf.cascadeCommandsAllowList": '
        f'["git push https://x:{fakes.GITHUB}@github.com/o/r"],\n}}\n',
    ),
    "hermes": (
        Path(".hermes") / "config.yaml",
        f"command_allowlist:\n  - git status\n  - export OPENAI_API_KEY={fakes.OPENAI}\n",
    ),
    "opencode": (
        Path(".config") / "opencode" / "opencode.json",
        json.dumps({"permission": {"bash": {f"curl -u {fakes.STRIPE}: x": "allow"}}}),
    ),
    "copilot": (
        _VSCODE / "Code" / "User" / "settings.json",
        json.dumps({"chat.tools.terminal.autoApprove": {f"git push {fakes.GITHUB}": True}}),
    ),
    "cursor": (
        Path(".cursor") / "cli-config.json",
        json.dumps({"permissions": {"allow": [f"Shell(export K={fakes.AWS})"]}}),
    ),
}


@pytest.mark.parametrize("agent", sorted(ALLOWLISTS))
def test_each_agents_allowlist_is_masked(tmp_path, agent):
    relative, text = ALLOWLISTS[agent]
    home = tmp_path / "home"
    path = _write(home / relative, text.encode())
    findings = [f for f in scan(home, None) if f.check_id == "AP002"]
    assert [f.agent for f in findings] == [agent]
    p = fix.plan(findings)
    [change] = p.changes
    assert fix.apply(p, home).applied == [change]
    key = next(k for k in fakes.ALL.values() if k in text)
    assert path.read_bytes() == _masked(text.encode(), key)
    assert [f for f in scan(home, None) if f.check_id == "AP002"] == []
