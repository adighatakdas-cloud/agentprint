"""Static checks on the landing page: a static path without motion, a terminal that can't
widen the page, fragments that come from the generator, and brand tokens instead of raw hex."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
TERMINAL = SITE / "src" / "components" / "Terminal.astro"
HERO = SITE / "src" / "components" / "Hero.astro"
LANDING_CSS = SITE / "src" / "styles" / "landing.css"
INDEX = SITE / "src" / "content" / "docs" / "index.mdx"

HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![0-9a-zA-Z])")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """The declarations of the first top-level rule whose selector list is exactly `selector`."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if m.group(1).strip() == selector:
            return m.group(2)
    raise AssertionError(f"no rule for {selector}")


def test_terminal_has_reduced_motion_path():
    src = _read(TERMINAL)
    assert "prefers-reduced-motion" in src
    assert "IntersectionObserver" in src
    # Replay ships hidden: without JS, or with reduced motion, there is nothing to replay.
    assert re.search(r"<button\b[^>]*\shidden[\s>]", src)
    # Even if a hidden line were left behind, reduced motion shows it.
    css = _read(LANDING_CSS)
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_terminal_cannot_widen_the_page():
    screen = _rule(_read(LANDING_CSS), ".ap-term-screen")
    assert "max-width: 100%" in screen
    assert re.search(r"overflow(-x)?: auto", screen)
    # The hero grid's columns may shrink below their content, so a wide panel scrolls inside.
    hero = _rule(_read(LANDING_CSS), ".ap-hero")
    assert "minmax(0, 1fr)" in hero


def test_terminal_uses_the_generated_fragments():
    src = _read(TERMINAL)
    for name in ("welcome", "scan", "agent"):
        assert re.search(rf'from "\.\./data/terminal/{name}\.html\?raw"', src), name
    assert "set:html" in src


def test_landing_uses_tokens_not_raw_hex():
    for path in (LANDING_CSS, HERO, TERMINAL):
        text = re.sub(r"var\(--[\w-]+\s*,[^()]*\)", "var()", _read(path))  # fallbacks may be hex
        assert not HEX_RE.findall(text), path.name


def test_works_inside_comes_from_the_roster():
    # The agent names are generated (tools/gen_docs.py writes agents.json), never retyped.
    src = _read(INDEX)
    assert "data/agents.json" in src
    assert "Antigravity" not in src and "Gemini CLI" not in src
