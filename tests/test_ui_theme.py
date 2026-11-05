from pathlib import Path

import pytest

import agentprint
from agentprint.models import Severity
from agentprint.ui.theme import SEVERITY_STYLE, console, is_light, theme


@pytest.mark.xfail(strict=True, reason="report.py still has green until Task 6")
def test_no_green_anywhere():
    root = Path(agentprint.__file__).parent
    for py in root.rglob("*.py"):
        assert "green" not in py.read_text(), py


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"COLORFGBG": "0;15"}, True),
        ({"COLORFGBG": "0;7"}, True),
        ({"COLORFGBG": "15;0"}, False),
        ({"AGENTPRINT_THEME": "light"}, True),
        ({}, False),
    ],
)
def test_light_detection(env, expected):
    assert is_light(env) is expected


def test_light_theme_uses_dark_amber():
    t = theme({"AGENTPRINT_THEME": "light"})
    assert t.styles["ap.amber"].color.triplet.hex == "#a35f00"


def test_severity_styles_exist():
    t = theme({})
    for sev in Severity:
        assert SEVERITY_STYLE[sev] in t.styles


def test_console_has_no_highlighting():
    c = console({}, record=True, force_terminal=True, width=80)
    c.print("0.41s ~/path 42")
    assert c.export_text(clear=False, styles=True) == c.export_text(clear=False)
