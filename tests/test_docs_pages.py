import re
import subprocess

from agentprint.secrets import find_secrets
from tools import gen_docs

DOCS = gen_docs.ROOT / "site/src/content/docs"
PAGES = [
    "get-started",
    "fix",
    "inside-your-agent",
    "ci",
    "badge",
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


def test_start_here_is_get_started_fix_inside_your_agent_ci_badge():
    config = (gen_docs.ROOT / "site/astro.config.mjs").read_text()
    start = re.search(r'label: "Start here",\s*items: \[(.*?)\]', config, re.S).group(1)
    slugs = re.findall(r'slug: "([^"]+)"', start)
    assert slugs == ["get-started", "fix", "inside-your-agent", "ci", "badge"]


def test_fix_page_covers_what_a_reader_needs():
    page = (DOCS / "fix.md").read_text()
    for needed in (
        "agentprint fix --yes --format agent",
        "agentprint fix --undo",
        "--dry-run",
        "--only",
        "--pack",
        "--fail-on",
        "Needs you",
        "${NAME}",
        "~/.agentprint/fix/",
        "sk-a…(len 105)",
        "## Exit codes",
    ):
        assert needed in page, needed
    for verb in ("mask", "env", "ignore", "lock"):
        assert f"`{verb}`" in page, verb
    for word in ("print", "sweep", "preflight", "masked", "BLOCK", "CLEAR"):
        assert re.search(rf"\b{word}\b", page), word
    assert "/agentprint/glossary/" in page


def test_get_started_points_at_fix():
    page = (DOCS / "get-started.md").read_text()
    assert "## Fix what it finds" in page
    assert "uvx agentprint fix" in page and "(/agentprint/fix/)" in page
    assert "| `agentprint fix` |" in page


def test_badge_page_covers_the_verifiable_badge():
    text = (DOCS / "badge.md").read_text()
    assert "verifiable" in text
    for banned in ("unfakeable", "encrypted"):
        assert banned not in text.lower()
    assert "https://img.shields.io/endpoint?url=" in text
    assert "agentprint-badge/badge.json" in text
    assert "/attestations" in text
    assert "gh attestation verify badge.json -R OWNER/REPO" in text
    for flag in (
        "--predicate-type https://adighatakdas-cloud.github.io/agentprint/badge/v1",
        "--source-ref refs/heads/main",
        "--source-digest <commit>",
        "--signer-workflow OWNER/REPO/.github/workflows/agentprint.yml",
        "git ls-remote https://github.com/OWNER/REPO refs/heads/main",
    ):
        assert flag in text, flag
    assert "does not prove the workflow ran agentprint unchanged" in text
    assert "byte-identical" in text
    assert "persist-credentials: false" in text
    assert "branches: [main]" in text and "pull_request:" not in text.split("```yaml")[1]
    assert "fail-on: none" in text and "never publish" in text
    for needle in (
        "badge-dir",
        "attest-badge: true",
        "publish-badge: true",
        "id-token: write",
        "attestations: write",
        "contents: write",
        "private",
        "default branch",
    ):
        assert needle in text, needle
    assert "adighatakdas-cloud/agentprint@v0.1.0  # better: pin the release's commit SHA" in text
    assert "![" not in text.split("```")[0] and "<img" not in text
    assert "/agentprint/badge/" in (DOCS / "ci.md").read_text()


def test_no_page_hardcodes_the_check_count():
    """The Checks page is generated; other pages must not say how many checks there are."""
    words = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\\d+"
    count = re.compile(rf"\b(?:{words})\s+checks\b|\bAP001\s+(?:to|through|–|-)\s+AP0\d\d", re.I)
    for path in DOCS.rglob("*.md"):
        if "checks" in path.relative_to(DOCS).parts[:1]:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            assert not count.search(line), f"{path.name}:{number}: {line.strip()}"
