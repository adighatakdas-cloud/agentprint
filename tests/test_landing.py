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
AUDIT = SITE / "scripts" / "audit-dist.mjs"

HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![0-9a-zA-Z])")
# Starlight's --sl-content-pad-x at 50rem and wider (@astrojs/starlight style/props.css).
STARLIGHT_PAD_PX = 24
# JetBrains Mono advances every glyph 600/1000 em, so 80 columns are 48em.
COLUMNS_80_EM = 80 * 0.6


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """The declarations of the first top-level rule whose selector list is exactly `selector`."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if m.group(1).strip() == selector:
            return m.group(2)
    raise AssertionError(f"no rule for {selector}")


def _media(css: str, query: str) -> str:
    """The body of the first `@media <query> { ... }` block."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    start = css.index(f"@media {query} {{") + len(f"@media {query} {{")
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        i += 1
    return css[start : i - 1]


def test_terminal_has_reduced_motion_path():
    src = _read(TERMINAL)
    assert "prefers-reduced-motion" in src
    assert "IntersectionObserver" in src
    # Replay ships hidden: without JS, or with reduced motion, there is nothing to replay.
    assert re.search(r"<button\b[^>]*\shidden[\s>]", src)
    # Even if a hidden line were left behind, reduced motion shows it.
    css = _read(LANDING_CSS)
    assert re.search(r"@media [^{]*\(prefers-reduced-motion: reduce\)", css)


def test_terminal_cannot_widen_the_page():
    screen = _rule(_read(LANDING_CSS), ".ap-term-screen")
    assert "max-width: 100%" in screen
    assert re.search(r"overflow(-x)?: auto", screen)
    # The hero grid's columns may shrink below their content, so a wide panel scrolls inside.
    hero = _rule(_read(LANDING_CSS), ".ap-hero")
    assert "minmax(0, 1fr)" in hero


def test_terminal_uses_the_generated_fragments():
    src = _read(TERMINAL)
    for name in ("welcome", "scan", "fix", "agent"):
        assert re.search(rf'from "\.\./data/terminal/{name}\.html\?raw"', src), name
    assert "set:html" in src


def test_the_story_goes_block_fix_clear_then_the_agent():
    """welcome -> $ agentprint scan -> BLOCK -> $ agentprint fix -> CLEAR -> the agent's view."""
    src = _read(TERMINAL)
    frontmatter = src.split("---")[1]
    assert re.search(r'const FIX = "agentprint fix";', frontmatter)
    screen = frontmatter[frontmatter.index("const screen = [") :]
    order = [screen.index(f'block("{name}"') for name in ("welcome", "scan", "fix", "agent")]
    assert order == sorted(order)
    assert re.search(r'block\("fix", \[command\(FIX\)', screen)
    caption = r'row\("what your AI agent reads", " ap-caption"\)'
    assert re.search(r'block\("agent", \[\s*command\(PACK\),\s*' + caption, screen)
    assert "CLEAR" in _read(TERMINAL.parent.parent / "data" / "terminal" / "fix.html")


def test_the_answer_is_typed_like_a_command():
    """The y/N question stays real text; only its answer is uncovered by typing, and each
    command's output is the lines up to the next command."""
    src = _read(TERMINAL)
    frontmatter, script = src.split("---")[1], src[src.index("<script is:inline>") :]
    assert r"const ASK = /^(Apply \d+ fix(?:es)?\? \[y\/N\] )(y)$/;" in frontmatter
    assert 'class="ap-typed">${' in frontmatter and '" ap-cmd ap-answer"' in frontmatter
    assert "ap-cmd" in script and "ap-answer" in script


def test_the_built_page_must_carry_the_whole_story():
    # audit-dist fails a build whose static landing lost a step of the story.
    audit = _read(AUDIT)
    texts = re.search(r"const LANDING_TEXT = \[(.*?)\];", audit, re.DOTALL).group(1)
    for needed in ("BLOCK", "agentprint fix", "Apply 3 fixes? [y/N]", "CLEAR", "agentprint: BLOCK"):
        assert f'"{needed}"' in texts, needed


def test_landing_uses_tokens_not_raw_hex():
    for path in (LANDING_CSS, HERO, TERMINAL):
        text = re.sub(r"var\(--[\w-]+\s*,[^()]*\)", "var()", _read(path))  # fallbacks may be hex
        assert not HEX_RE.findall(text), path.name


def test_works_inside_comes_from_the_roster():
    # The agent names are generated (tools/gen_docs.py writes agents.json), never retyped.
    src = _read(INDEX)
    assert "data/agents.json" in src
    assert "Antigravity" not in src and "Gemini CLI" not in src


def test_two_columns_start_where_80_columns_fit():
    """At the two-column breakpoint the terminal's column holds all 80 columns at the font
    floor; anywhere narrower the hero's terminal would scroll sideways (it did at 960-1095 px)."""
    css = re.sub(r"/\*.*?\*/", "", _read(LANDING_CSS), flags=re.DOTALL)
    m = re.search(r"@media \(min-width: ([\d.]+)rem\) \{\s*\.ap-hero \{([^}]*)\}", css)
    assert m, "the two-column .ap-hero rule"
    breakpoint = float(m.group(1)) * 16
    fr = re.search(r"minmax\(0, ([\d.]+)fr\) minmax\(0, ([\d.]+)fr\)", m.group(2))
    left, right = float(fr.group(1)), float(fr.group(2))
    gap = float(re.search(r"gap: ([\d.]+)rem", m.group(2)).group(1)) * 16
    screen = _rule(css, ".ap-term-screen")
    floor, pad, slack = re.search(
        r"clamp\(([\d.]+)px, \(100cqi - ([\d.]+)rem - ([\d.]+)px\) / 48, [\d.]+px\)", screen
    ).groups()
    floor, pad, slack = float(floor), float(pad) * 16, float(slack)
    # The clamp's padding is the panel's real side padding, and the border is 1 px.
    assert re.search(rf"padding: [\d.]+rem {pad / 2 / 16:g}rem;", screen)
    assert "border: 1px solid" in _rule(css, ".ap-term")
    column = (breakpoint - 2 * STARLIGHT_PAD_PX - gap) * right / (left + right)
    needed = COLUMNS_80_EM * floor + pad + slack + 2
    assert column >= needed, f"{column:.0f} px column, 80 columns at {floor:g} px need {needed:.0f}"


def test_the_real_run_survives_any_script_state():
    """The script never rewrites the generated text, and print or reduced motion shows it all;
    an error mid-run gives up to the static run with nothing to replay."""
    src = _read(TERMINAL)
    script = src[src.index("<script is:inline>") :]
    assert ".textContent =" not in script and "welcome.innerHTML" not in script
    assert re.search(r"catch \(e\) \{\s*if \(e === STOP\) return;\s*giveUp\(\);", script)
    give_up = re.search(r"function giveUp\(\) \{(.*?)\n    \}", script, re.DOTALL).group(1)
    assert "replay.hidden = true" in give_up and "finish()" in give_up
    fallback = _media(_read(LANDING_CSS), "print, (prefers-reduced-motion: reduce)")
    for selector, declaration in (
        (".ap-term .ap-off", "visibility: visible"),
        (".ap-term .ap-sweep", "display: none"),
        (".ap-term .ap-typing .ap-typed", "width: auto"),
    ):
        assert re.search(rf"{re.escape(selector)}[^{{]*\{{[^}}]*{declaration}", fallback), selector
