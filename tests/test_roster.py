import json

from agentprint.roster import AgentState, roster, summary_line


def _state(home, *agents):
    d = home / ".agentprint"
    d.mkdir()
    files = {f"/x/{i}": {"agent": a} for i, a in enumerate(agents)}
    (d / "state.json").write_text(json.dumps({"version": 1, "files": files, "dirs": []}))


def test_roster_marks_armed_from_state(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    _state(tmp_path, "claude-code")
    by = {s.name: s for s in roster(tmp_path, {})}
    assert by["claude-code"].found and by["claude-code"].armed
    assert by["cursor"].found and not by["cursor"].armed
    assert not by["codex"].found and not by["codex"].armed


def _s(name, found=True, armed=False):
    return AgentState(name, name, found, armed)


def test_summary_line_variants():
    four = [_s("a", armed=True), _s("b", armed=True), _s("Cursor"), _s("Gemini CLI")]
    assert summary_line(four) == "2 of 4 agents armed · Cursor, Gemini CLI are not"
    assert summary_line([_s("a", armed=True), _s("b", armed=True)]) == "2 of 2 agents armed"
    assert (
        summary_line([_s("a", armed=True), _s("Cursor")]) == "1 of 2 agents armed · Cursor is not"
    )
    assert summary_line([_s("a", found=False)]) == "no AI agents found on this machine"
    assert summary_line([]) == "no AI agents found on this machine"
