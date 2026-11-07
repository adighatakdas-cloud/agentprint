import re

import agentprint
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
    wanted = (
        "uvx agentprint scan",
        "uvx agentprint fix",
        "uvx agentprint install",
        "uvx agentprint pack",
    )
    for text in (*wanted, "npx agentprint-cli"):
        assert text in README
    # The "Pre-release" note stays until the first release (docs/RELEASING.md step 1).
    pre_release = re.search(r"(dev|a|b|rc)\d*$", agentprint.__version__) is not None
    notes = sum(x.startswith("> **Pre-release:**") for x in README.splitlines())
    assert notes == (1 if pre_release else 0)


def test_quickstart_fixes_right_after_the_sweep():
    block = README.split("## Quickstart")[1].split("```bash\n")[1].split("```")[0]
    lines = block.splitlines()
    assert [x.split("#")[0].split()[2] for x in lines] == ["scan", "fix", "install", "pack"]
    fix = lines[1]
    assert "#" in fix and len(fix) <= 80, fix
    assert len({x.index("#") for x in lines}) == 1  # the comments line up
