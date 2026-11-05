import pytest

from agentprint.structured import find_line, load, strip_jsonc


def test_loads_plain_json(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text('{"permissions": {"allow": ["Bash(ls)"]}}')
    assert load(f) == {"permissions": {"allow": ["Bash(ls)"]}}


def test_loads_toml_by_suffix(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('approval_policy = "never"\n[mcp_servers.x]\ncommand = "npx"\n')
    assert load(f) == {"approval_policy": "never", "mcp_servers": {"x": {"command": "npx"}}}


def test_loads_jsonc_with_comments_and_trailing_commas(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(
        '{\n  // a comment\n  "a": "http://x//y", /* block */\n'
        '  "b": [1, 2,],\n  "c": "/* not */",\n}\n'
    )
    assert load(f) == {"a": "http://x//y", "b": [1, 2], "c": "/* not */"}


def test_strip_jsonc_keeps_escaped_quotes_in_strings():
    assert strip_jsonc('{"a": "say \\"hi\\" // ok"} // tail') == '{"a": "say \\"hi\\" // ok"} '


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("broken.json", '{"permissions": {"allow": ["Bash('),
        ("broken.toml", "approval_policy = \n[[["),
        ("deep.json", "[" * 100_000),
        ("empty.json", ""),
    ],
)
def test_broken_files_return_none_and_never_raise(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(text)
    assert load(f) is None


def test_missing_binary_and_directory_return_none(tmp_path):
    (tmp_path / "bin.json").write_bytes(b'{"a": "\x00"}')
    assert load(tmp_path / "bin.json") is None
    assert load(tmp_path / "missing.json") is None
    assert load(tmp_path) is None


def test_reload_after_change(tmp_path):
    f = tmp_path / "s.json"
    f.write_text('{"a": 1}')
    assert load(f) == {"a": 1}
    f.write_text('{"a": 22}')
    assert load(f) == {"a": 22}


def test_find_line_points_at_first_occurrence(tmp_path):
    f = tmp_path / "s.json"
    f.write_text('{\n  "allow": [\n    "Bash(*)"\n  ]\n}\n')
    assert find_line(f, "Bash(*)") == 3
    assert find_line(f, "nope") is None
    assert find_line(f, "") is None


def test_loads_yaml_by_suffix(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("approvals:\n  mode: smart\nmcp_servers:\n  gh:\n    command: npx\n")
    assert load(f) == {"approvals": {"mode": "smart"}, "mcp_servers": {"gh": {"command": "npx"}}}
    g = tmp_path / "x.yml"
    g.write_text("- a\n- b\n")
    assert load(g) == ["a", "b"]


def test_yaml_tags_that_build_objects_are_refused(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("!!python/object/apply:os.getcwd []\n")
    assert load(f) is None


def test_yaml_alias_bomb_is_refused(tmp_path):
    lines = ["l0: &l0 [x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 25):
        refs = ", ".join([f"*l{i - 1}"] * 9)
        lines.append(f"l{i}: &l{i} [{refs}]")
    f = tmp_path / "bomb.yaml"
    f.write_text("\n".join(lines) + "\n")
    assert load(f) is None


def test_a_few_yaml_aliases_are_fine(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("base: &b {timeout: 10}\nhooks:\n  pre_tool_call:\n    - *b\n")
    assert load(f) == {"base": {"timeout": 10}, "hooks": {"pre_tool_call": [{"timeout": 10}]}}


def test_broken_yaml_returns_none(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("hooks:\n  - [unclosed\n")
    assert load(f) is None
