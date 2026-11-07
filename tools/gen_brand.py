"""Draw the brand files from the mascot pixel maps and the JetBrains Mono wordmark.

    uv run python -m tools.gen_brand            # write every file
    uv run python -m tools.gen_brand --check    # list stale files, exit 1 if any

Every output is a pure function of the pixel maps, the palette and the font, so
the files are byte-identical on any machine.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

from agentprint.ui.mascot import FULL, SMALL

ROOT = Path(__file__).resolve().parents[1]
FONT = Path(__file__).resolve().parent / "fonts" / "JetBrainsMono-ExtraBold.ttf"

PALETTE: dict[str, dict[str, str]] = {
    "dark": {"body": "#FF3B4E", "frame": "#FFB020", "agent": "#F3F6FA", "print": "#FFB020"},
    "light": {"body": "#C8102E", "frame": "#A35F00", "agent": "#06121F", "print": "#A35F00"},
}
GROUND = "#050B14"
WORD = ("agent", "print")

# size -> (mascot pixel, gap to the wordmark, wordmark cap height)
_SIZES = {"full": (8, 28, 56), "small": (4, 10, 22)}


def _num(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _runs(rows: Sequence[str]) -> list[tuple[int, int, int, str]]:
    """Horizontal runs of same-kind cells as (row, first column, length, kind)."""
    runs = []
    for y, row in enumerate(rows):
        x = 0
        while x < len(row):
            kind = row[x]
            n = 1
            while x + n < len(row) and row[x + n] == kind:
                n += 1
            if kind != ".":
                runs.append((y, x, n, kind))
            x += n
    return runs


def _mascot_group(
    rows: Sequence[str], pixel: int, x: float, y: float, attrs: dict[str, str]
) -> str:
    """One <g> of <rect> runs; `attrs` maps a cell kind (A/R) to its fill or class attribute."""
    rects = "".join(
        f'<rect {attrs[k]} x="{_num(x + cx * pixel)}" y="{_num(y + cy * pixel)}" '
        f'width="{n * pixel}" height="{pixel}"/>'
        for cy, cx, n, k in _runs(rows)
    )
    return f'<g data-pixel="{pixel}" shape-rendering="crispEdges">{rects}</g>'


def _fills(mode: str) -> dict[str, str]:
    pal = PALETTE[mode]
    return {"A": f'fill="{pal["body"]}"', "R": f'fill="{pal["frame"]}"'}


def _svg(view_box: str, body: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}">{body}</svg>\n'


def mascot_svg(rows: Sequence[str], mode: str, pixel: int) -> str:
    """The mascot alone, transparent background."""
    w, h = max(len(r) for r in rows) * pixel, len(rows) * pixel
    return _svg(f"0 0 {w} {h}", _mascot_group(rows, pixel, 0, 0, _fills(mode)))


class _Wordmark:
    """The word 'agentprint' outlined from the font, in font units (y up)."""

    def __init__(self) -> None:
        font = TTFont(FONT)
        os2: Any = font["OS/2"]  # fontTools' table classes are untyped
        self.cap = os2.sCapHeight
        self.xh = os2.sxHeight
        cmap, glyphs, hmtx = font.getBestCmap(), font.getGlyphSet(), font["hmtx"]
        if cmap is None:
            raise ValueError(f"{FONT} has no character map")
        self.paths: dict[str, str] = {}
        left = right = bottom = top = None
        pen_x = 0
        for part in WORD:
            pen = SVGPathPen(glyphs, ntos=lambda v: str(round(v)))
            for ch in part:
                name = cmap[ord(ch)]
                glyphs[name].draw(TransformPen(pen, (1, 0, 0, 1, pen_x, 0)))
                bounds = BoundsPen(glyphs)
                glyphs[name].draw(bounds)
                if bounds.bounds is None:
                    raise ValueError(f"glyph {name!r} has no outline")
                x0, y0, x1, y1 = bounds.bounds
                left = pen_x + x0 if left is None else left
                right = pen_x + x1
                bottom = y0 if bottom is None else min(bottom, y0)
                top = y1 if top is None else max(top, y1)
                pen_x += hmtx[name][0]
            self.paths[part] = pen.getCommands()
        if left is None or right is None or bottom is None or top is None:
            raise ValueError("the wordmark has no glyphs")
        self.left, self.right, self.bottom, self.top = left, right, bottom, top


def logo_svg(mode: str, size: str) -> str:
    """Mascot, then the wordmark, on a transparent background."""
    pixel, gap, cap = _SIZES[size]
    rows = FULL if size == "full" else SMALL
    pal = PALETTE[mode]
    mw, mh = max(len(r) for r in rows) * pixel, len(rows) * pixel
    wm = _Wordmark()
    s = cap / wm.cap
    baseline = mh / 2 + wm.xh * s / 2  # x-height centred on the mascot's middle
    x0 = mw + gap - wm.left * s  # ink starts exactly `gap` after the mascot
    top = min(0, baseline - wm.top * s)
    bottom = max(mh, baseline - wm.bottom * s)
    width = mw + gap + (wm.right - wm.left) * s
    # scale keeps 5 decimals: glyph coordinates are large integers
    transform = f'transform="translate({_num(x0)} {_num(baseline)}) scale({s:.5f} {-s:.5f})"'
    word = "".join(f'<path fill="{pal[p]}" {transform} d="{wm.paths[p]}"/>' for p in WORD)
    view_box = f"0 {_num(top)} {_num(width)} {_num(bottom - top)}"
    return _svg(view_box, _mascot_group(rows, pixel, 0, 0, _fills(mode)) + word)


def avatar_svg() -> str:
    pixel = 30
    w, h = len(FULL[0]) * pixel, len(FULL) * pixel
    mascot = _mascot_group(FULL, pixel, (512 - w) / 2, (512 - h) / 2, _fills("dark"))
    ground = f'<rect width="512" height="512" rx="64" fill="{GROUND}"/>'
    return _svg("0 0 512 512", ground + mascot)


def favicon_svg() -> str:
    dark, light = PALETTE["dark"], PALETTE["light"]
    pixel = 4
    w, h = len(SMALL[0]) * pixel, len(SMALL) * pixel
    style = (
        f"<style>.b{{fill:{dark['body']}}}.f{{fill:{dark['frame']}}}"
        "@media (prefers-color-scheme: light)"
        f"{{.b{{fill:{light['body']}}}.f{{fill:{light['frame']}}}}}</style>"
    )
    mascot = _mascot_group(
        SMALL, pixel, (32 - w) / 2, (32 - h) / 2, {"A": 'class="b"', "R": 'class="f"'}
    )
    return _svg("0 0 32 32", style + mascot)


SHARE_URL = "adighatakdas-cloud.github.io/agentprint"
SHARE_TAGLINE = "The security check your AI coding agent runs for you."
# Colours the share image uses beyond PALETTE["dark"] (spec: text, dim text, clear/low).
TEXT, DIM, CLEAR = "#E8EEF5", "#8DA2B5", "#5CC8FF"


@cache
def _font() -> tuple[Any, Any, Any, int]:
    font = TTFont(FONT)
    cmap = font.getBestCmap()
    if cmap is None:
        raise ValueError(f"{FONT} has no character map")
    head: Any = font["head"]  # fontTools' table classes are untyped
    return cmap, font.getGlyphSet(), font["hmtx"], head.unitsPerEm


def _outline(text: str, em: float, x: float, baseline: float, fill: str) -> tuple[str, float]:
    """`text` as one outlined <path> with its left edge at x; returns (svg, width in px).

    Outlined, not <text>, so it looks the same in every viewer. The mono font gives every
    glyph the same advance, so the width is len(text) * advance.
    """
    cmap, glyphs, hmtx, units = _font()
    pen = SVGPathPen(glyphs, ntos=lambda v: str(round(v)))
    pen_x = 0
    for ch in text:
        name = cmap[ord(ch)]
        glyphs[name].draw(TransformPen(pen, (1, 0, 0, 1, pen_x, 0)))
        pen_x += hmtx[name][0]
    s = em / units
    transform = f'transform="translate({_num(x)} {_num(baseline)}) scale({s:.5f} {-s:.5f})"'
    label = text.replace("&", "&amp;").replace('"', "&quot;")
    path = f'<path aria-label="{label}" fill="{fill}" {transform} d="{pen.getCommands()}"/>'
    return path, pen_x * s


def share_svg() -> str:
    """The 1280x640 social preview: mascot, wordmark, tagline, BLOCK -> fix -> CLEAR, URL."""
    pal = PALETTE["dark"]
    margin, pixel = 96, 16
    mw, mh = max(len(r) for r in FULL) * pixel, len(FULL) * pixel
    my = 92
    wm = _Wordmark()
    cap = 84
    s = cap / wm.cap
    wx = margin + mw + 44 - wm.left * s
    baseline = my + mh / 2 + wm.xh * s / 2
    transform = f'transform="translate({_num(wx)} {_num(baseline)}) scale({s:.5f} {-s:.5f})"'
    word = "".join(f'<path fill="{pal[p]}" {transform} d="{wm.paths[p]}"/>' for p in WORD)
    parts = [
        "<title>agentprint: the security check your AI coding agent runs for you</title>",
        f'<rect width="1280" height="640" fill="{GROUND}"/>',
        _mascot_group(FULL, pixel, margin, my, _fills("dark")),
        word,
        _outline(SHARE_TAGLINE, 33, margin, 366, TEXT)[0],
        f'<rect x="{margin}" y="420" width="1088" height="2" fill="{DIM}" opacity="0.35"/>',
    ]
    x = margin
    strip = (
        ("BLOCK", pal["body"]),
        (" → ", DIM),
        ("agentprint fix", pal["frame"]),
        (" → ", DIM),
        ("CLEAR", CLEAR),
    )
    for text, fill in strip:
        path, width = _outline(text, 46, x, 506, fill)
        parts.append(path)
        x += width
    parts.append(_outline(SHARE_URL, 24, margin, 584, DIM)[0])
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 640" '
        f'width="1280" height="640">{"".join(parts)}</svg>\n'
    )


def outputs() -> dict[Path, str]:
    """Every generated file: absolute path -> file text."""
    files = {
        "docs/brand/logo-dark.svg": logo_svg("dark", "full"),
        "docs/brand/logo-light.svg": logo_svg("light", "full"),
        "docs/brand/avatar.svg": avatar_svg(),
        "docs/brand/social-preview.svg": share_svg(),
        "site/src/assets/logo-small-dark.svg": logo_svg("dark", "small"),
        "site/src/assets/logo-small-light.svg": logo_svg("light", "small"),
        "site/public/favicon.svg": favicon_svg(),
    }
    return {ROOT / rel: text for rel, text in files.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gen_brand", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report stale files, write nothing")
    args = parser.parse_args(argv)
    stale = 0
    for path, text in outputs().items():
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        if args.check:
            print(path)
            stale += 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
