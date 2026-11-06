import re

from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.roster import SHORT_NAMES
from tools.gen_docs import DOCS_URL, ROOT

README = (ROOT / "README.md").read_text()
RAW = "https://raw.githubusercontent.com/adighatakdas-cloud/agentprint/main/docs/brand/"


def test_readme_is_short():
    assert len(README.splitlines()) <= 140


def test_hero_switches_logo_by_theme():
    assert "<picture>" in README
    dark = re.search(r'<source media="\(prefers-color-scheme: dark\)" srcset="([^"]+)"', README)
    light = re.search(r'<img alt="agentprint" src="([^"]+)"', README)
    assert dark and dark.group(1) == RAW + "logo-dark.svg"
    assert light and light.group(1) == RAW + "logo-light.svg"


def test_every_url_is_absolute():
    targets = re.findall(r"\]\(([^)\s]+)\)", README)
    targets += re.findall(r'(?:src|href|srcset)="([^"]*)"', README)
    assert targets
    bad = [t for t in targets if not (t.startswith("https://") or t.startswith("#"))]
    assert bad == []


def test_docs_links_point_at_real_pages():
    slugs = set(re.findall(re.escape(DOCS_URL) + r"([a-z0-9/-]*?)/?[)\"\s]", README))
    slugs.discard("")
    assert slugs
    docs = ROOT / "site" / "src" / "content" / "docs"
    for slug in slugs:
        candidates = (docs / f"{slug}.md", docs / f"{slug}.mdx", docs / slug / "index.md")
        assert any(p.exists() for p in candidates), slug
    assert "census/pilot-2026-11" in slugs


def test_works_inside_lists_every_integration():
    line = next(x for x in README.splitlines() if "Claude Code" in x and " · " in x)
    for name in ALL_INTEGRATIONS:
        assert SHORT_NAMES[name] in line, name


def test_quickstart_and_install_lines():
    wanted = ("uvx agentprint scan", "uvx agentprint install", "uvx agentprint pack")
    for text in (*wanted, "npx agentprint-cli"):
        assert text in README
    assert sum(x.startswith("> **Pre-release:**") for x in README.splitlines()) == 1
