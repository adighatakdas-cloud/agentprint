import xml.etree.ElementTree as ET

import pytest

from agentprint.ui.mascot import FULL, SMALL
from tools import gen_brand

MODES = ("dark", "light")
SVG_NS = "{http://www.w3.org/2000/svg}"


def _cells(rows):
    return sum(c != "." for r in rows for c in r)


def _rect_cells(svg: str, fill: str) -> int:
    """Pixel cells drawn in `fill`, counting width/pixel for each rect run."""
    root = ET.fromstring(svg)
    total = 0
    for g in root.iter(f"{SVG_NS}g"):
        if g.get("shape-rendering") != "crispEdges":
            continue
        pixel = float(g.get("data-pixel"))
        for r in g.iter(f"{SVG_NS}rect"):
            if (r.get("fill") or g.get("fill")) == fill:
                total += round(float(r.get("width")) / pixel)
    return total


def test_generated_files_are_current():
    stale = [p for p, text in gen_brand.outputs().items() if p.read_text() != text]
    assert stale == []  # run: uv run python -m tools.gen_brand


def test_outputs_are_absolute_paths():
    assert all(p.is_absolute() for p in gen_brand.outputs())


def test_palette_matches_spec():
    assert gen_brand.PALETTE["dark"] == {
        "body": "#FF3B4E",
        "frame": "#FFB020",
        "agent": "#F3F6FA",
        "print": "#FFB020",
    }
    assert gen_brand.PALETTE["light"] == {
        "body": "#C8102E",
        "frame": "#A35F00",
        "agent": "#06121F",
        "print": "#A35F00",
    }


@pytest.mark.parametrize("mode", MODES)
def test_full_logo_draws_every_pixel_of_full_mascot(mode):
    svg = gen_brand.logo_svg(mode, "full")
    pal = gen_brand.PALETTE[mode]
    body = sum(c == "A" for r in FULL for c in r)
    frame = sum(c == "R" for r in FULL for c in r)
    assert _rect_cells(svg, pal["body"]) == body
    assert _rect_cells(svg, pal["frame"]) == frame
    assert body + frame == _cells(FULL)


@pytest.mark.parametrize("mode", MODES)
def test_small_logo_has_no_frame(mode):
    svg = gen_brand.logo_svg(mode, "small")
    pal = gen_brand.PALETTE[mode]
    assert _rect_cells(svg, pal["body"]) == sum(c == "A" for r in SMALL for c in r)
    # the only frame-colored cell is the dot between the eyes
    assert _rect_cells(svg, pal["frame"]) == sum(c == "R" for r in SMALL for c in r) == 1


@pytest.mark.parametrize("mode", MODES)
def test_wordmark_is_outlined_not_text(mode):
    pal = gen_brand.PALETTE[mode]
    for text in (gen_brand.logo_svg(mode, "full"), gen_brand.logo_svg(mode, "small")):
        root = ET.fromstring(text)
        paths = [p for p in root.iter(f"{SVG_NS}path")]
        assert sorted(p.get("fill") for p in paths) == sorted([pal["agent"], pal["print"]])
    for text in gen_brand.outputs().values():
        assert "<text" not in text
        assert "font-family" not in text


def test_mascot_is_crisp():
    for text in gen_brand.outputs().values():
        root = ET.fromstring(text)
        groups = [g for g in root.iter(f"{SVG_NS}g") if g.get("data-pixel")]
        assert groups
        assert all(g.get("shape-rendering") == "crispEdges" for g in groups)
        assert not [r for r in root.iter(f"{SVG_NS}rect") if r.get("shape-rendering")]


def test_favicon_switches_for_light_tabs():
    svg = gen_brand.favicon_svg()
    assert "@media (prefers-color-scheme: light)" in svg
    for mode in MODES:
        assert gen_brand.PALETTE[mode]["body"] in svg
        assert gen_brand.PALETTE[mode]["frame"] in svg


def test_svgs_parse_and_have_viewbox():
    for path, text in gen_brand.outputs().items():
        root = ET.fromstring(text)
        vb = [float(v) for v in root.get("viewBox").split()]
        assert len(vb) == 4 and vb[2] > 0 and vb[3] > 0, path
        for dim in ("width", "height"):
            if root.get(dim) is not None:
                assert float(root.get(dim)) > 0, path


def test_light_logo_has_no_dark_mode_colors():
    svg = gen_brand.logo_svg("light", "full")
    for color in gen_brand.PALETTE["dark"].values():
        assert color not in svg


def test_check_flags_stale_file(tmp_path, monkeypatch, capsys):
    stale = tmp_path / "x.svg"
    stale.write_text("old")
    monkeypatch.setattr(gen_brand, "outputs", lambda: {stale: "new"})
    assert gen_brand.main(["--check"]) == 1
    assert str(stale) in capsys.readouterr().out
    assert stale.read_text() == "old"
    assert gen_brand.main([]) == 0
    assert stale.read_text() == "new"
