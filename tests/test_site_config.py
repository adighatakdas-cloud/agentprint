"""Static checks on the docs site config: brand rules, pins, deploy gate, packaging."""

import colorsys
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"

STYLE_SUFFIXES = {".css", ".mjs", ".astro", ".svg"}
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])")


def _walk_files():
    files = list((SITE / "src").rglob("*"))
    files += SITE.glob("*.mjs")
    files += (SITE / "public").glob("*.svg")
    files += (ROOT / "docs" / "brand").rglob("*")
    return [f for f in files if f.is_file()]


def _is_style_source(path: Path) -> bool:
    if path.suffix in STYLE_SUFFIXES:
        return True
    return path.suffix == ".html" and (SITE / "src" / "data") in path.parents


def _rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(h: str) -> float:
    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_no_green_anywhere():
    bad = []
    for path in _walk_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _is_style_source(path) and re.search(r"green|lime", text, re.IGNORECASE):
            bad.append(f"{path.relative_to(ROOT)}: green/lime keyword")
        for m in HEX_RE.finditer(text):
            h, _lightness, sat = colorsys.rgb_to_hls(*_rgb(m.group()))
            if 90 <= h * 360 <= 165 and sat > 0.25:
                bad.append(f"{path.relative_to(ROOT)}: {m.group()} is green-hued")
    assert not bad, bad


def _tokens(block_selectors: tuple[str, ...]) -> dict[str, str]:
    css = (SITE / "src" / "styles" / "brand.css").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = tuple(s.strip().replace('"', "'") for s in m.group(1).split(","))
        if any(s in block_selectors for s in selectors):
            for name, val in re.findall(r"(--ap-[a-z]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;", m.group(2)):
                out[name] = val
    return out


def test_text_contrast_both_modes():
    dark = _tokens((":root", ":root[data-theme='dark']"))
    light = dark | _tokens((":root[data-theme='light']",))
    assert dark["--ap-ground"].upper() == "#050B14"
    assert light["--ap-ground"].upper() == "#F3F8FB"
    for mode, tok in (("dark", dark), ("light", light)):
        for name in ("--ap-text", "--ap-dim", "--ap-strong", "--ap-amber"):
            r = _ratio(tok[name], tok["--ap-ground"])
            assert r >= 4.5, f"{mode} {name} {tok[name]} on ground is {r:.2f}:1"


def test_package_json_pins_exact_versions_and_disables_telemetry():
    pkg = json.loads((SITE / "package.json").read_text(encoding="utf-8"))
    for group in ("dependencies", "devDependencies"):
        for name, ver in pkg.get(group, {}).items():
            assert re.fullmatch(r"\d+\.\d+\.\d+", ver), f"{name} {ver}"
    assert pkg["packageManager"].startswith("pnpm@")
    for name, cmd in pkg["scripts"].items():
        if "astro " in cmd:
            assert cmd.startswith("ASTRO_TELEMETRY_DISABLED=1 "), name


def test_astro_config_targets_project_pages():
    cfg = (SITE / "astro.config.mjs").read_text(encoding="utf-8")
    assert 'site: "https://adighatakdas-cloud.github.io"' in cfg
    assert 'base: "/agentprint"' in cfg
    assert 'trailingSlash: "always"' in cfg
    assert "starlight-links-validator" in cfg
    assert "./src/styles/brand.css" in cfg and "./src/styles/fonts.css" in cfg


def _job_block(text: str, job: str) -> str:
    m = re.search(rf"^  {job}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert m, job
    return m.group(1)


def test_docs_workflow_is_pinned_and_deploys_only_when_public():
    text = (ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*-?\s*uses:\s*(.+)$", text, re.MULTILINE)
    assert uses
    for line in uses:
        assert re.search(r"@[0-9a-f]{40} # v", line), line
    deploy = _job_block(text, "deploy")
    cond = re.search(r"^\s+if:\s*(.+)$", deploy, re.MULTILINE).group(1)
    assert "!github.event.repository.private" in cond
    assert "refs/heads/main" in cond
    top = text.split("\njobs:")[0]
    assert re.search(r"^permissions:\n  contents: read\n", top, re.MULTILINE)
    assert "pages: write" not in top and "id-token" not in top
    assert "pages: write" in deploy and "id-token: write" in deploy
    assert "pages: write" not in _job_block(text, "build")


def test_site_is_not_packaged():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    include = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert not any(i.startswith(("/site", "/tools")) for i in include)
