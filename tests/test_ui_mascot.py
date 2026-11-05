from agentprint.ui.mascot import FULL, SMALL, half_blocks
from agentprint.ui.theme import HEX, LIGHT_HEX


def test_small_mascot_plain_text():
    assert [t.plain for t in half_blocks(SMALL)] == [
        "   █   ",
        "▄█████▄",
        "██▄▀▄██",
        "▀▄███▄▀",
    ]


def test_amber_dot_is_two_colored():
    line = half_blocks(SMALL)[2]
    assert line.plain[3] == "▀"
    spans = [s for s in line.spans if s.start <= 3 < s.end]
    assert len(spans) == 1
    assert spans[0].style == f"{HEX['amber']} on {HEX['red']}"


def test_light_uses_light_hex():
    line = half_blocks(SMALL, light=True)[2]
    styles = {str(s.style) for s in line.spans}
    assert f"{LIGHT_HEX['amber']} on {LIGHT_HEX['red']}" in styles


def test_full_has_frame():
    assert FULL[0].startswith("RRR")
    assert len(FULL) == 11
    assert all(len(r) == 13 for r in FULL)
    assert len(half_blocks(FULL)) == 6
    assert all(len(r) == 7 for r in SMALL) and len(SMALL) == 8
