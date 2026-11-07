import re
import subprocess

from agentprint.secrets import find_secrets
from tools import gen_docs

DOCS = gen_docs.ROOT / "site/src/content/docs"
PAGES = [
    "get-started",
    "inside-your-agent",
    "ci",
    "agents",
    "how-it-works",
    "privacy",
    "glossary",
    "comparison",
    "census/index",
    "census/pilot-2026-11",
]
MOVED = ["docs/supported-agents.md", "docs/VOLUNTEER_SCAN.md", "docs/pilot-2026-11.md"]


def _frontmatter(path):
    match = re.match(r"---\n(.*?)\n---\n", path.read_text(), re.S)
    assert match, path
    return dict(re.findall(r"^(\w+):\s*(.+)$", match.group(1), re.M))


def test_pages_exist_with_title_and_description():
    for page in PAGES + ["404"]:
        meta = _frontmatter(DOCS / f"{page}.md")
        assert meta["title"].strip("\"'"), page
        assert 0 < len(meta["description"].strip("\"'")) <= 160, page


def test_glossary_defines_every_lexicon_word():
    glossary = (DOCS / "glossary.md").read_text()
    for word in [
        "print",
        "sweep",
        "preflight",
        "tripwire",
        "armed",
        "unarmed",
        "roster",
        "BLOCK",
        "CLEAR",
        "masked",
        "brief",
        "census",
    ]:
        assert re.search(rf"^\*\*{word}\*\*|^### {word}\b", glossary, re.M | re.I), word


def test_moved_files_are_gone_and_unreferenced():
    for name in MOVED:
        assert not (gen_docs.ROOT / name).exists(), name
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=gen_docs.ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    needles = ["supported-agents.md", "VOLUNTEER_SCAN.md", "pilot-2026-11.md"]
    for name in tracked:
        if name == "tests/test_docs_pages.py" or name.startswith("docs/superpowers/"):
            continue
        path = gen_docs.ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for needle in needles:
            assert needle not in text, f"{name} mentions {needle}"


def test_internal_links_use_the_base_path():
    for path in DOCS.rglob("*.md*"):
        for target in re.findall(r"\]\((/[^)\s]*)\)", path.read_text()):
            assert target.startswith("/agentprint/"), f"{path.name}: {target}"


def test_no_full_keys_in_pages():
    files = list(DOCS.rglob("*.md*")) + list((gen_docs.ROOT / "site/check-notes").glob("*.md"))
    for path in files:
        assert find_secrets(path.read_text()) == [], path.name
