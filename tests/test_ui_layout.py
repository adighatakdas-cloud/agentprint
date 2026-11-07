"""Wrapping for the human screens: under a hanging indent, never mid-word."""

from rich.text import Text

from agentprint.ui.layout import MIN_ROOM, fit_middle, hang, wrap


def _plain(lines):
    return [line.plain for line in lines]


def test_no_width_means_one_line():
    assert _plain(hang("  ", "a " * 80, None)) == ["  " + "a " * 80]


def test_continuation_lines_start_under_the_body():
    lines = _plain(hang("lead: ", "one two three four five six seven eight nine ten", 26))
    assert lines == ["lead: one two three four", "      five six seven eight", "      nine ten"]
    assert all(len(ln) <= 26 for ln in lines)


def test_breaks_after_a_dot_separator_and_keeps_parts_whole():
    text = "4 prints · 1 critical · 1 high · 1 medium · 1 low · keys masked"
    lines = _plain(wrap(Text(text), 32))
    assert lines == ["4 prints · 1 critical · 1 high ·", "1 medium · 1 low · keys masked"]


def test_a_part_too_wide_wraps_by_words():
    text = "1 of 6 agents armed · Codex, Cursor, Gemini CLI, OpenCode, Copilot are not"
    assert _plain(wrap(Text(text), 30)) == [
        "1 of 6 agents armed · Codex,",
        "Cursor, Gemini CLI, OpenCode,",
        "Copilot are not",
    ]


def test_masked_key_stub_never_splits():
    text = "Anthropic API key in a prompt: sk-a…(len 105) masked"
    lines = _plain(wrap(Text(text), 36))
    assert "sk-a…(len 105) masked" in lines[-1]


def test_a_word_wider_than_the_room_is_cut():
    word = "/" + "d" * 40
    lines = _plain(wrap(Text(f"chmod go-rwx {word}"), 20))
    assert lines[0] == "chmod go-rwx"
    assert "".join(lines[1:]) == word and all(len(ln) <= 20 for ln in lines)


def test_styles_survive_wrapping():
    body = Text("plain ")
    body.append("amber words here", style="ap.amber")
    lines = wrap(body, MIN_ROOM)
    styled = [ln.plain[s.start : s.end] for ln in lines for s in ln.spans]
    assert "".join(styled).replace(" ", "") == "amberwordshere"


def test_never_narrower_than_min_room():
    lines = _plain(hang(" " * 30, "a b c d e f g h i j k l m n o p", 32))
    assert all(len(ln) <= 30 + MIN_ROOM for ln in lines)


def test_fit_middle_keeps_the_end():
    assert fit_middle("~/short", 20) == "~/short"
    got = fit_middle("~/work/clients/acme/monorepo/packages/the-app", 24)
    assert len(got) == 24 and "…" in got and got.endswith("packages/the-app")
    assert fit_middle("abc", 1) == "…"
