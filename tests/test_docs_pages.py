import re
import subprocess

from agentprint import __version__
from agentprint.plugins import REMOVE_PLUGIN
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
    "plugins",
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
    assert (
        f"adighatakdas-cloud/agentprint@v{__version__}  # better: pin the release's commit SHA"
        in text
    )
    assert "![" not in text.split("```")[0] and "<img" not in text
    assert "/agentprint/badge/" in (DOCS / "ci.md").read_text()


def test_badge_page_offers_both_modes_and_recommends_signed_by_agentprint():
    text = (DOCS / "badge.md").read_text()
    assert "## Signed by agentprint (new in v0.1.0)" in text
    assert "## Signed by your own workflow" in text
    assert text.index("## Signed by agentprint") < text.index("## Signed by your own workflow")
    assert f"uses: adighatakdas-cloud/agentprint/.github/workflows/badge.yml@v{__version__}" in text
    assert "--signer-workflow adighatakdas-cloud/agentprint/.github/workflows/badge.yml" in text
    assert "works from v0.1.0" in text
    assert "supports `scan` only" in text and "`command: pack` is refused" in text
    assert "uvx --no-config" in text and "runs none of your code" in text
    assert "`path`" in text and "`command`" in text and "`fail_on`" in text
    assert "proves" in text and "agentprint's workflow file" in text
    assert "cannot edit" not in text
    assert "recommended" not in text.lower().split("## signed by your own workflow")[0]
    assert "--signer-digest <release commit SHA>" in text and "imposter commit" in text
    assert "release's notes" in text
    assert "cannot edit" not in (gen_docs.ROOT / ".github/workflows/badge.yml").read_text()
    assert "fail-on: none" in text and "refused" in text


def test_no_page_hardcodes_the_check_count():
    """The Checks page is generated; other pages must not say how many checks there are."""
    words = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\\d+"
    count = re.compile(rf"\b(?:{words})\s+checks\b|\bAP001\s+(?:to|through|–|-)\s+AP0\d\d", re.I)
    for path in DOCS.rglob("*.md"):
        if "checks" in path.relative_to(DOCS).parts[:1]:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            assert not count.search(line), f"{path.name}:{number}: {line.strip()}"


PUBLIC = gen_docs.ROOT / "site/public"
# A root-relative link or image to a file (it has an extension) under the site's base path.
ASSET = re.compile(r"""(?:\]\(|\b(?:src|href)=["'])/agentprint/([^)"'\s#?]+\.[A-Za-z0-9]+)""")


def test_every_asset_a_docs_page_references_is_in_site_public():
    pages = [*DOCS.rglob("*.md"), *DOCS.rglob("*.mdx")]
    referenced = {(p.name, a) for p in pages for a in ASSET.findall(p.read_text())}
    assert ("fix.md", "agent-loop.gif") in referenced  # the pattern sees the GIF
    missing = sorted((page, asset) for page, asset in referenced if not (PUBLIC / asset).is_file())
    assert missing == []


def test_agent_loop_gif_is_the_same_file_in_docs_and_on_the_site():
    gif = (gen_docs.ROOT / "docs/agent-loop.gif").read_bytes()
    assert gif[:6] in (b"GIF87a", b"GIF89a")
    assert gif == (PUBLIC / "agent-loop.gif").read_bytes()


def test_ci_page_pins_users_copy_name_this_release():
    text = (DOCS / "ci.md").read_text()
    assert f"uses: adighatakdas-cloud/agentprint@v{__version__}  #" in text
    assert f"rev: v{__version__}\n" in text
    assert re.findall(r"agentprint@v(\d+\.\d+\.\d+)", text) == [__version__]


def test_pinned_versions_in_ci_and_plugin_pages_are_this_release():
    version = r"(\d+\.\d+\.\d+(?:[a-z]+\d+|\.dev\d+)?)"
    for page in ("ci", "plugins"):
        text = (DOCS / f"{page}.md").read_text()
        pins = re.findall(r"(?:agentprint==|--branch v|--ref v)" + version, text)
        assert pins and set(pins) == {__version__}, page
    assert "uvx agentprint==" in (DOCS / "ci.md").read_text()  # lefthook and husky


def test_ci_page_has_pre_push_lefthook_and_husky():
    text = (DOCS / "ci.md").read_text()
    for needed in (
        "agentprint-pre-push",
        "pre-commit install --hook-type pre-push",
        "lefthook.yml",
        "lefthook install",
        ".husky/pre-push",
    ):
        assert needed in text, needed


def test_plugins_page_covers_every_ecosystem_and_says_pick_one():
    text = (DOCS / "plugins.md").read_text()
    assert "Use the plugin or `agentprint install`, not both" in text
    agents = ["Claude Code", "Codex", "GitHub Copilot CLI", "Gemini CLI", "Cursor", "Antigravity"]
    for agent in [*agents, "Hermes Agent", "VS Code"]:
        assert f"### {agent}" in text or f"| {agent}" in text, agent
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and ln.endswith(" |")]
    status = [
        ln.rsplit("|", 2)[1].strip()
        for ln in rows
        if "as documented" in ln or "docs-verified only" in ln
    ]
    assert status.count("as documented") == 3 and len(status) == 8
    for word in ("code --add-mcp", "vscode:mcp/install?", "macOS and Linux only", "uninstall"):
        assert word in text, word
    for command in REMOVE_PLUGIN.values():  # the same way out status prints
        assert command in text.replace("`", ""), command


def test_plugins_page_is_in_the_reference_sidebar():
    config = (gen_docs.ROOT / "site/astro.config.mjs").read_text()
    reference = re.search(r'label: "Reference",\s*items: \[(.*?)\]', config, re.S).group(1)
    slugs = re.findall(r'slug: "([^"]+)"', reference)
    assert slugs[:2] == ["agents", "plugins"]
