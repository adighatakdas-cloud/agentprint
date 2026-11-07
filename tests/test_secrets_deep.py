"""Keys hidden on purpose: invisible characters, split strings, folded lines and base64.

Every key comes from tests.fakes and is cut, joined or encoded here at runtime, so no
key-shaped literal (plain, split or encoded) is ever in the source.
"""

import base64
import json
import sqlite3
import time
from pathlib import Path

import pytest

from agentprint import fix, secrets
from agentprint.advice import rotate_text
from agentprint.checks.ap001_history_secret import CHECK as AP001
from agentprint.checks.ap002_settings_secret import CHECK as AP002
from agentprint.checks.ap006_package_leak import check_listing
from agentprint.checks.base import safe_snippet
from agentprint.models import Artifact, Kind
from agentprint.pack import PackageListing
from agentprint.scanner import scan
from agentprint.secrets import find_hidden, find_secrets, find_secrets_deep, redact
from tests import fakes

KEY = fakes.ANTHROPIC
A, B = KEY[:20], KEY[20:]  # two halves: neither is a key on its own


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _only(text: str) -> secrets.SecretMatch:
    """The one hidden match in `text`, after checking a plain scan misses it."""
    assert find_secrets(text) == []
    [match] = find_hidden(text)
    return match


# ---- each way of hiding a key ----------------------------------------------------------------


@pytest.mark.parametrize("char", ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"])
def test_invisible_character_inside_a_key(char):
    text = f"my key is {A}{char}{B} thanks"
    match = _only(text)
    assert (match.rule_id, match.value, match.obfuscation) == ("anthropic", KEY, "invisible")
    assert match.start == text.index(A)


def test_invisible_characters_between_every_character():
    text = "key: " + "\u200b".join(fakes.GITHUB)
    match = _only(text)
    assert (match.value, match.obfuscation) == (fakes.GITHUB, "invisible")


@pytest.mark.parametrize(
    "joined",
    [
        f'"{A}" + "{B}"',  # JavaScript, Python, Java
        f"'{A}' + '{B}'",
        f'"{A}"+"{B}"',
        f'"{A}" . "{B}"',  # PHP, Perl
        f'"{A}" .. "{B}"',  # Lua
        f'"{A}" "{B}"',  # C, Python implicit concatenation
        f"'{A}'\"{B}\"",  # shell
    ],
)
def test_split_string_literals(joined):
    text = f"const k = {joined};"
    match = _only(text)
    assert (match.value, match.obfuscation) == (KEY, "split")
    assert match.start == text.index(A)


def test_split_string_in_three_pieces():
    key = fakes.OPENAI
    text = f'x = "{key[:10]}" + "{key[10:30]}" + "{key[30:]}"'
    assert _only(text).value == key


@pytest.mark.parametrize(
    "joined",
    [
        f'(\n    "{A}"\n    "{B}"\n)',  # Python: implicit concatenation inside parentheses
        f'"{A}" +\n    "{B}"',  # Java, JavaScript: operator at the end of the line
        f'"{A}"\r\n    + "{B}"',  # CRLF line endings
    ],
)
def test_split_string_over_lines(joined):
    text = f"KEY = {joined}\n"
    match = _only(text)
    assert (match.value, match.obfuscation) == (KEY, "split")
    assert text.count("\n", 0, match.start) == text.count("\n", 0, text.index(A))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_folded_keys_with_either_line_ending(newline):
    yaml = newline.join(["env:", "  TOKEN: |", f"    {A}", f"    {B}", "x: 1", ""])
    shell = newline.join([f"export K={A}\\", f"{B}", ""])
    for text in (yaml, shell):
        assert (_only(text).value, _only(text).obfuscation) == (KEY, "folded")


def test_split_generic_secret_keeps_its_name():
    value = fakes.GENERIC_VALUE
    match = _only(f'api_key = "{value[:12]}" + "{value[12:]}"')
    assert (match.rule_id, match.value, match.obfuscation) == ("generic", value, "split")


def test_backslash_line_continuation():
    text = f"echo hi\nexport CLAUDE_KEY={A}\\\n    {B}\n"
    match = _only(text)
    assert (match.value, match.obfuscation) == (KEY, "folded")
    assert text.count("\n", 0, match.start) == 1  # the line the key starts on


@pytest.mark.parametrize("indicator", [">", "|", ">-", "|+", "|2"])
def test_yaml_block_scalar(indicator):
    text = f"mcp:\n  env:\n    TOKEN: {indicator}\n      {A}\n      {B}\n  other: 1\n"
    match = _only(text)
    assert (match.value, match.obfuscation) == (KEY, "folded")
    assert text.count("\n", 0, match.start) == 3


def test_yaml_list_item_block():
    text = f"keys:\n  - |\n    {A}\n    {B}\n"
    assert _only(text).value == KEY


def test_yaml_block_generic_value_on_the_next_line():
    value = fakes.GENERIC_VALUE
    match = _only(f"api_key: >\n  {value}\nname: x\n")
    assert (match.rule_id, match.value, match.obfuscation) == ("generic", value, "folded")


def test_yaml_block_ends_at_the_parent_indent():
    # The next key is not part of the block: nothing is joined onto it.
    text = f"a: |\n  {A}\n{B}: 1\n"
    assert find_hidden(text) == []


@pytest.mark.parametrize(
    "template",
    [
        "echo {} | base64 -d",
        'Authorization: Basic {}"',
        '{{"KEY_B64": "{}"}}',
        "data={}",
    ],
)
def test_base64_blob(template):
    blob = _b64(f"ANTHROPIC_API_KEY={KEY}\n")
    text = template.format(blob)
    match = _only(text)
    assert (match.value, match.obfuscation) == (KEY, "base64")
    assert (match.start, match.end) == (text.index(blob), text.index(blob) + len(blob))


def test_base64_basic_auth_pair():
    blob = _b64("user:" + fakes.GITHUB)
    assert _only(f"-H 'Authorization: Basic {blob}'").value == fakes.GITHUB


def test_base64_after_a_json_newline_escape():
    blob = _b64(f"key {KEY}")
    line = json.dumps({"display": f"here:\n{blob}\nthanks"})
    assert _only(line).value == KEY


# ---- the same tricks inside JSON strings (transcripts, chat logs) ----------------------------


def test_split_string_inside_a_json_transcript_line():
    code = f'const k = "{A}" + "{B}";\nconsole.log(k);'
    line = "hello\n" + json.dumps({"type": "assistant", "content": code}) + "\n"
    match = _only(line)
    assert (match.value, match.obfuscation) == (KEY, "split")
    assert line.count("\n", 0, match.start) == 1


def test_escaped_invisible_character_inside_json():
    line = json.dumps({"display": f"{A}\u200b{B}"})  # ensure_ascii: written as \u200b
    assert "\\u200b" in line
    match = _only(line)
    assert (match.value, match.obfuscation) == (KEY, "invisible")


def test_yaml_block_inside_a_json_string():
    line = json.dumps({"content": f"config.yaml:\nenv:\n  KEY: |\n    {A}\n    {B}\n"})
    match = _only(line)
    assert (match.value, match.obfuscation) == (KEY, "folded")


def test_continuation_inside_a_json_string():
    line = json.dumps({"content": f"export K={A}\\\n{B}"})
    assert _only(line).value == KEY


# ---- what is reported, and what is not -------------------------------------------------------


def test_direct_matches_are_unchanged_and_not_hidden():
    [match] = find_secrets(f"key {KEY}")
    assert match.obfuscation is None
    assert find_secrets_deep(f"key {KEY}") == [match]


def test_deep_returns_direct_and_hidden_in_order():
    text = f"{fakes.GITHUB}\n" + f'"{A}" + "{B}"\n'
    found = find_secrets_deep(text)
    assert [(m.value, m.obfuscation) for m in found] == [(fakes.GITHUB, None), (KEY, "split")]


def test_key_also_found_directly_is_reported_once():
    text = f"{KEY}\n" + f'"{A}" + "{B}"\n'
    assert [m.obfuscation for m in find_secrets_deep(text)] == [None]
    assert find_hidden(text, known={KEY}) == []


def test_same_hidden_key_twice_is_one_match():
    text = f'"{A}" + "{B}"\n' * 3
    assert [m.value for m in find_hidden(text)] == [KEY]


@pytest.mark.parametrize(
    "text",
    [
        'greeting = "Hello, " + name + "!"',
        'x = "abc" + "def" + "ghi"',
        'print("a" "b"); s = "x" . "y"',
        "\ufeff# a file that starts with a byte-order mark\nname: demo\n",
        "word\u200bjoiner and soft\u00adhyphen",
        "script: |\n  set -e\n  npm ci\n  npm test\n",
        "description: >\n  A long sentence that is folded\n  over several lines.\n",
        "cmd = ls \\\n  -la \\\n  /tmp\n",
        _b64("just some ordinary text that is long enough to be decoded, no keys at all"),
        base64.b64encode(bytes(range(256)) * 4).decode(),  # binary, such as an image
        "sha256:9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a39281706f5e4d3c2b1a0",
        "/Users/someone/projects/AgentPrintDemo/tests/fixtures/DataFiles/x.json",
        json.dumps({"content": 'line one\nline two\n\ttabbed "quoted" text'}),
        '{"a": "b", "c": "d"}',
    ],
)
def test_ordinary_text_hides_nothing(text):
    assert find_hidden(text) == []


def test_a_key_already_visible_inside_a_block_is_not_hidden():
    text = f"KEY: |\n  {KEY}\n"
    assert [m.obfuscation for m in find_secrets_deep(text)] == [None]


# ---- limits ----------------------------------------------------------------------------------


def test_base64_blob_count_is_capped():
    filler = " ".join(_b64(f"ordinary text number {i:04} padded out to length") for i in range(300))
    secret = _b64(f"key {KEY}")
    assert find_hidden(f"{secret} {filler}")
    assert find_hidden(f"{filler} {secret}") == []  # past MAX_BLOBS: not decoded


def test_base64_decodes_at_most_64_kb_per_blob():
    assert secrets.MAX_BLOB_CHARS <= 64 * 1024
    late = _b64("x" * 70_000 + f" {KEY}")
    early = _b64(f"{KEY} " + "x" * 70_000)
    assert find_hidden(late) == []
    assert find_hidden(early)[0].value == KEY


HOSTILE = {
    "invisible every other character": "a\u200b" * 2_500_000,
    "invisible run": "\u200b" * 5_000_000,
    "one giant split string": '"a"+' * 1_250_000,
    "quotes only": '"' * 5_000_000,
    "unclosed quotes": ('"' + "a" * 99) * 50_000,
    "continuations": "a\\\n" * 1_666_666,
    "giant yaml block": "k: |\n" + "  ab\n" * 1_000_000,
    "yaml headers": "k: |\n" * 1_000_000,
    "one base64 blob": "QUJD" * 1_250_000,
    "many base64 blobs": (_b64("ordinary text " * 4) + " ") * 60_000,
    "json escapes": '"\\n' * 1_666_666,
    "sparse joins": ('"x" + "y"' + " " * 600) * 8_000,
    "literals on lines of their own": '"a"\n' * 1_250_000,
    "blank lines after each literal": ('"a"' + "\n" * 997) * 5_000,
    "indented literals": '"a"\n  ' * 830_000,
    # Joins all through the text: every view is searched, generic rules included.
    "joins among words": ('"x" + "y"' + "abcdefghij" * 60) * 8_000,
    "joins among generic names": ('"x" + "y"' + "token_ab " * 66) * 8_000,
}


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_input_is_fast(name):
    text = HOSTILE[name][:5_000_000]
    start = time.perf_counter()
    find_hidden(text)
    assert time.perf_counter() - start < 1.0


def test_key_on_a_window_boundary_is_found():
    # Joins every ~600 characters merge into views that stop growing at MAX_WINDOW; a key split
    # across the edge of one is found in the next, which reaches back over it.
    unit = '"x" + "y"' + "abcdefghij" * 59
    for shift in range(0, 700, 13):
        text = unit * 107 + "q" * shift + f'k = "{A}" + "{B}"; ' + unit * 5
        assert [m.value for m in find_hidden(text) if m.value == KEY] == [KEY], shift


# ---- in the checks ---------------------------------------------------------------------------


def _hidden_detail(key: str, kind: str) -> str:
    return f"Anthropic API key: {redact(key)} (hidden: {kind})"


def test_history_check_reports_a_hidden_key(tmp_path):
    f = tmp_path / "history.jsonl"
    f.write_text(json.dumps({"display": "hi"}) + "\n" + json.dumps({"display": f'"{A}" + "{B}"'}))
    [finding] = AP001.run([Artifact("claude-code", Kind.HISTORY, f)])
    assert (finding.check_id, finding.line) == ("AP001", 2)
    assert finding.detail == _hidden_detail(KEY, "split string")
    assert KEY not in repr(finding)


def test_settings_check_reports_a_folded_key(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(f"model: x\nenv:\n  ANTHROPIC_API_KEY: >\n    {A}\n    {B}\n")
    [finding] = AP002.run([Artifact("hermes", Kind.SETTINGS, f)])
    assert (finding.check_id, finding.line) == ("AP002", 4)
    assert finding.detail == _hidden_detail(KEY, "folded lines")


def test_check_reports_a_key_once_when_it_is_also_plain(tmp_path):
    f = tmp_path / "history.jsonl"
    f.write_text(f'{KEY}\n"{A}" + "{B}"\n')
    [finding] = AP001.run([Artifact("claude-code", Kind.HISTORY, f)])
    assert finding.detail == f"Anthropic API key: {redact(KEY)}"


def test_check_finds_base64_and_invisible_kinds(tmp_path):
    f = tmp_path / "history.jsonl"
    f.write_text(f"{_b64('k=' + fakes.GITHUB)}\n{fakes.AWS[:8]}\u200b{fakes.AWS[8:]}\n")
    found = {x.detail for x in AP001.run([Artifact("codex", Kind.HISTORY, f)])}
    assert found == {
        f"GitHub token: {redact(fakes.GITHUB)} (hidden: base64)",
        f"AWS access key ID: {redact(fakes.AWS)} (hidden: invisible characters)",
    }


def test_check_finds_a_hidden_key_past_the_first_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "DEEP_CHUNK_CHARS", 1000)
    f = tmp_path / "history.jsonl"
    filler = "".join(f'{{"n": {i}, "text": "nothing to see here"}}\n' for i in range(200))
    f.write_text(filler + f'k = "{A}" + "{B}"\n' + filler)
    [finding] = AP001.run([Artifact("claude-code", Kind.HISTORY, f)])
    assert finding.line == 201
    f.write_text(filler + f"k = {A}\\\n{B}\n")  # a continuation across a chunk's last line
    [finding] = AP001.run([Artifact("claude-code", Kind.HISTORY, f)])
    assert (finding.line, finding.detail) == (201, _hidden_detail(KEY, "folded lines"))


def test_check_finds_a_hidden_key_in_a_chat_database(tmp_path):
    db = tmp_path / "state.vscdb"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    con.execute("INSERT INTO ItemTable VALUES (?, ?)", ("chat", f'"{A}" + "{B}"'))
    con.commit()
    con.close()
    [finding] = AP001.run([Artifact("cursor", Kind.HISTORY, db)])
    assert (finding.line, finding.detail) == (None, _hidden_detail(KEY, "split string"))


def test_package_scan_finds_a_hidden_key_in_an_agent_file():
    member = "package/.claude/settings.local.json"
    data = json.dumps({"env": {"K": "x"}}, indent=2) + f'\n"{A}" "{B}"\n'
    listing = PackageListing("pkg-1.0.0.tgz", [member], {member: data.encode()})
    findings = check_listing(listing)
    assert [f.check_id for f in findings] == ["AP006", "AP002"]
    assert findings[1].detail == _hidden_detail(KEY, "split string")
    assert findings[1].line == 6
    assert findings[1].title == "Secret in agent file inside package"


# ---- output never shows a hidden key -------------------------------------------------------


def test_safe_snippet_redacts_hidden_keys():
    for text in (f'k = "{A}" + "{B}"', f"echo {_b64('k=' + KEY)} | base64 -d"):
        shown = safe_snippet(text, 400)
        assert redact(KEY) in shown
        assert A not in shown and B not in shown and _b64("k=" + KEY) not in shown


def test_rotate_text_names_the_key_without_the_hidden_note(tmp_path):
    f = tmp_path / "history.jsonl"
    f.write_text(f'"{A}" + "{B}"\n')
    [finding] = AP001.run([Artifact("claude-code", Kind.HISTORY, f)])
    text = rotate_text(finding)
    assert f'starts "{redact(KEY)}":' in text and "hidden" not in text


# ---- fix: a hidden key is never masked, it is the human's -----------------------------------


def _history_home(tmp_path: Path, text: str) -> tuple[Path, Path]:
    home = tmp_path / "home"
    path = home / ".claude" / "history.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return home, path


def test_fix_leaves_a_hidden_key_to_the_human(tmp_path):
    original = json.dumps({"display": f'"{A}" + "{B}"'}) + "\n"
    home, path = _history_home(tmp_path, original)
    p = fix.plan(scan(home, None))
    assert p.changes == []
    [item] = [m for m in p.todo if m.check_id == "AP001"]
    assert "hidden" in item.text and "split string" in item.text
    assert "by hand" in item.text and "Rotate" in item.text
    assert "hidden" in item.person and "Rotate" in item.person
    outcome = fix.apply(p, home)
    assert outcome.applied == [] and path.read_text() == original
    assert KEY not in repr(p) and A not in repr(p)


def test_fix_masks_the_plain_key_and_leaves_the_hidden_one(tmp_path):
    original = (
        json.dumps({"display": f"plain {fakes.GITHUB}"})
        + "\n"
        + json.dumps({"display": f"{A}\u200b{B}"}, ensure_ascii=False)
        + "\n"
    )
    home, path = _history_home(tmp_path, original)
    p = fix.plan(scan(home, None))
    [change] = p.changes
    assert change.shown == redact(fakes.GITHUB)
    assert any("hidden" in m.text and "invisible characters" in m.text for m in p.todo)
    fix.apply(p, home)
    after = path.read_text()
    assert fakes.GITHUB not in after
    assert f"{A}\u200b{B}" in after  # the hidden key is left exactly as it was


def test_fix_leaves_a_hidden_settings_key_to_the_human(tmp_path):
    home = tmp_path / "home"
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    text = json.dumps({"permissions": {"allow": [f"Bash(echo {_b64('k=' + KEY)})"]}})
    path.write_text(text)
    p = fix.plan(scan(home, None))
    assert p.changes == []
    assert any(m.check_id == "AP002" and "base64" in m.text for m in p.todo)
    assert path.read_text() == text


@pytest.mark.parametrize("npm_project", [True, False])
def test_fix_pack_leaves_a_hidden_key_to_the_human(tmp_path, npm_project):
    root = tmp_path / "my-app"
    (root / ".claude").mkdir(parents=True)
    if npm_project:  # fix reaches the source file; otherwise only the package copy
        (root / "package.json").write_text('{"name": "my-app", "version": "1.0.0"}')
    source = root / ".claude" / "settings.local.json"
    source.write_text(json.dumps({"permissions": {"allow": [f'Bash(k="{A}" "{B}")']}}))
    listing = PackageListing(
        "my-app-1.0.0.tgz",
        [".claude/settings.local.json"],
        {".claude/settings.local.json": source.read_bytes()},
    )
    before = source.read_bytes()
    p = fix.plan(check_listing(listing), pack=True, root=root)
    assert not [c for c in p.changes if c.kind == "mask"]
    [item] = [m for m in p.todo if m.check_id == "AP002"]
    assert "hidden (split string)" in item.text and "by hand" in item.text
    fix.apply(p, tmp_path / "home")
    assert source.read_bytes() == before
