"""Every terminal color lives here. Nothing else in agentprint picks a color."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TextIO

from rich.console import Console
from rich.theme import Theme

from agentprint.models import Severity

HEX: dict[str, str] = {
    "amber": "#FFB020",
    "red": "#FF3B4E",
    "dim": "#6F8397",
    "faint": "#1E2E42",
    "clear": "#5CC8FF",
    "critical": "#FF3B4E",
    "high": "#FF7A2F",
    "medium": "#E9C46A",
    "low": "#5CC8FF",
}

LIGHT_HEX: dict[str, str] = {**HEX, "amber": "#A35F00", "red": "#C8102E"}

_INK = "#050B14"


def is_light(env: Mapping[str, str]) -> bool:
    if env.get("AGENTPRINT_THEME") == "light":
        return True
    fgbg = env.get("COLORFGBG", "")
    return fgbg.rsplit(";", 1)[-1] in ("7", "15") if fgbg else False


def theme(env: Mapping[str, str]) -> Theme:
    hx = LIGHT_HEX if is_light(env) else HEX
    styles: dict[str, str] = {
        "ap.amber": hx["amber"],
        "ap.red": hx["red"],
        "ap.dim": hx["dim"],
        "ap.faint": hx["faint"],
        "ap.clear": hx["clear"],
        "ap.strong": "bold",
        # rich resolves a theme name only when it is the whole style string ("bold ap.amber"
        # silently renders unstyled), so every compound style gets a name of its own here.
        "ap.brand": f"bold {hx['amber']}",
        "ap.block": f"bold {_INK} on {hx['red']}",
        "ap.clear_label": f"bold {_INK} on {hx['clear']}",
    }
    for name in ("critical", "high", "medium", "low"):
        styles[f"ap.sev.{name}"] = f"bold {hx[name]}"
    return Theme(styles)


def console(
    env: Mapping[str, str],
    file: TextIO | None = None,
    plain: bool = False,
    **kwargs: Any,
) -> Console:
    if plain:  # --plain: no color, and no bold or reverse codes either
        kwargs.setdefault("no_color", True)
        kwargs.setdefault("force_terminal", False)
    # Otherwise no_color stays unset, so rich honours a NO_COLOR environment variable.
    # soft_wrap stays off: the screens build their lines to fit (ui/layout.py) and print the
    # few lines that name a path with soft_wrap=True themselves.
    return Console(file=file, theme=theme(env), highlight=False, **kwargs)


SEVERITY_STYLE: dict[Severity, str] = {s: f"ap.sev.{s.name.lower()}" for s in Severity}
