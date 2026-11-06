import html
import json
import re

import pytest

from agentprint.checks import SCAN_CHECKS
from agentprint.checks import ap006_package_leak as ap006
from agentprint.secrets import find_secrets
from tools import gen_docs

SITE = gen_docs.ROOT / "site"
IDS = [f"AP00{n}" for n in range(1, 9)]


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def test_generated_files_are_current():
    stale = [p for p, text in gen_docs.outputs().items() if p.read_text() != text]
    assert stale == []  # run: uv run python -m tools.gen_docs


def test_outputs_are_absolute_paths():
    assert all(p.is_absolute() for p in gen_docs.outputs())


def test_every_check_has_a_page_with_code_metadata():
    assert [d.id for d in gen_docs.check_docs()] == IDS
    for check in SCAN_CHECKS:  # title and fix are copied from code, never retyped
        page = (SITE / f"src/content/docs/checks/{check.id.lower()}.md").read_text()
        assert check.title in page and check.fix in page and check.severity.name.lower() in page
    page = (SITE / "src/content/docs/checks/ap006.md").read_text()
    assert ap006.TITLE in page and ap006.FIX in page and "high, medium or low" in page
    ap008 = (SITE / "src/content/docs/checks/ap008.md").read_text()
    assert "medium; high for key stores" in ap008


def test_checks_json_feeds_the_landing_page():
    data = json.loads((SITE / "src/data/checks.json").read_text())
    assert [d["id"] for d in data] == IDS
    assert data[2]["slug"] == "ap003"
    assert set(data[0]) == {"id", "title", "severity", "summary", "slug"}


def test_readme_table_links_each_check_page():
    readme = (gen_docs.ROOT / "README.md").read_text()
    block = readme.split(gen_docs.README_START)[1].split(gen_docs.README_END)[0]
    rows = [line for line in block.splitlines() if re.match(r"\| \[AP00\d", line)]
    assert len(rows) == 8
    for row, check_id in zip(rows, IDS, strict=True):
        assert f"{gen_docs.DOCS_URL}checks/{check_id.lower()}/" in row


def test_readme_outside_the_markers_is_untouched():
    new = gen_docs.outputs()[gen_docs.ROOT / "README.md"]
    old = (gen_docs.ROOT / "README.md").read_text()
    assert new.split(gen_docs.README_START)[0] == old.split(gen_docs.README_START)[0]
    assert new.split(gen_docs.README_END)[1] == old.split(gen_docs.README_END)[1]


def test_missing_readme_marker_fails_loudly():
    with pytest.raises(ValueError, match="checks:start"):
        gen_docs.replace_block("no markers here", "x")


def test_fragments_show_real_renderer_output():
    f = gen_docs.terminal_fragments()
    assert "BLOCK" in f["scan"] and "masked" in f["scan"]
    assert "agentprint: BLOCK" in f["agent"]
    assert "#ff3b4e" in f["welcome"].lower()  # red mascot body
    assert "<TMP>" not in f["scan"] and "~/code/my-app" in f["scan"]
    assert "agentprint scan  ~ + . · roster 3 · 0.38s" in _text(f["scan"])


def test_fragments_never_show_a_full_key():
    for name, fragment in gen_docs.terminal_fragments().items():
        assert find_secrets(_text(fragment)) == [], name


def test_fragments_have_no_green():
    for fragment in gen_docs.terminal_fragments().values():
        assert not re.search(r"green|lime", fragment, re.I)


def test_fragments_ignore_ambient_env(monkeypatch):
    before = gen_docs.terminal_fragments()
    for k, v in {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "40", "FORCE_COLOR": "0"}.items():
        monkeypatch.setenv(k, v)
    assert gen_docs.terminal_fragments() == before


def test_check_notes_have_required_sections():
    for doc in gen_docs.check_docs():
        assert 0 < len(doc.summary) <= 140, doc.id
        assert doc.runs_in, doc.id
        assert set(doc.runs_in) <= {"scan", "pack", "guard hook", "git push check"}
        headings = re.findall(r"^## (.+)$", doc.body, re.M)
        assert headings == ["What it finds", "Why it matters", "How to fix", "Limits"], doc.id


def test_agents_page_points_at_ap003_page():
    text = (gen_docs.ROOT / "site/src/content/docs/agents.md").read_text()
    assert "| Over-broad permission |" not in text
    assert "/agentprint/checks/ap003/" in text
