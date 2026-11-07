import html
import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint import __version__, cli
from agentprint import fix as fixer
from agentprint.checks import SCAN_CHECKS
from agentprint.checks import ap006_package_leak as ap006
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.roster import SHORT_NAMES
from agentprint.secrets import find_secrets
from tests import fakes
from tests.conftest import home_vars, set_home
from tools import gen_docs

SITE = gen_docs.ROOT / "site"
IDS = [f"AP00{n}" for n in range(1, 10)]


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


# The committed fragments are POSIX renderings (permission fixes and the AP008 sweep cannot be
# reproduced on NTFS); Windows checks the engine-vs-fragment tests below instead.
@pytest.mark.posix_only
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


def test_agents_json_is_the_roster_in_roster_order():
    data = json.loads((SITE / "src/data/agents.json").read_text())
    assert data == [label for name, label in SHORT_NAMES.items() if name in ALL_INTEGRATIONS]
    assert len(data) == len(ALL_INTEGRATIONS)


def test_readme_table_links_each_check_page():
    readme = (gen_docs.ROOT / "README.md").read_text()
    block = readme.split(gen_docs.README_START)[1].split(gen_docs.README_END)[0]
    rows = [line for line in block.splitlines() if re.match(r"\| \[AP00\d", line)]
    assert len(rows) == 9
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


def test_ci_page_pins_come_from_templates():
    for name, template in gen_docs.CI_BLOCKS.items():
        text = (gen_docs.ROOT / "tools" / "templates" / template).read_text()
        assert "{{version}}" in text and not re.search(r"\d+\.\d+\.\d+", text), template
        page = gen_docs.outputs()[SITE / "src/content/docs/ci.md"]
        assert page.count(f"<!-- {name}:start -->") == 1


def test_plugins_page_block_and_plugin_files_are_generated():
    files = gen_docs.outputs()
    page = files[SITE / "src/content/docs/plugins.md"]
    block = page.split(gen_docs.PLUGINS_START)[1].split(gen_docs.PLUGINS_END)[0]
    assert f"agentprint=={__version__}" in block
    assert gen_docs.ROOT / "server.json" in files
    assert gen_docs.ROOT / "plugins/agentprint/.claude-plugin/plugin.json" in files
    with pytest.raises(ValueError, match="plugins.md needs exactly one <!-- plugins:start -->"):
        gen_docs.replace_block(
            "none", "x", gen_docs.PLUGINS_START, gen_docs.PLUGINS_END, "plugins.md"
        )


def test_fragments_show_real_renderer_output():
    f = gen_docs.terminal_fragments()
    assert list(f) == ["welcome", "scan", "fix", "agent"]  # the landing's order
    assert "BLOCK" in f["scan"] and "masked" in f["scan"]
    assert "agentprint: BLOCK" in f["agent"]
    assert "#ff3b4e" in f["welcome"].lower()  # red mascot body
    assert "<TMP>" not in f["scan"] and "~/code/my-app" in f["scan"]
    assert "agentprint scan  ~ + . · roster 3 · 0.38s" in _text(f["scan"])


def test_fragments_never_show_a_full_key():
    for name, fragment in gen_docs.terminal_fragments().items():
        text = _text(fragment)
        assert find_secrets(text) == [], name
        assert "FAKE" not in text, name  # the demo keys' bodies: only masked stubs show
        for key in fakes.ALL.values():
            assert key not in text, name


def test_fragments_have_no_green():
    for fragment in gen_docs.terminal_fragments().values():
        assert not re.search(r"green|lime", fragment, re.I)


def test_fragments_ignore_ambient_env(monkeypatch, tmp_path):
    before = gen_docs.terminal_fragments()
    # A Gemini folder with a key in it: if the sweep followed GEMINI_CLI_HOME, it would show.
    gemini = tmp_path / "gemini" / ".gemini"
    gemini.mkdir(parents=True)
    (gemini / "settings.json").write_text(json.dumps({"apiKey": fakes.ALL["openai"]}))
    ambient = {
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "40",
        "FORCE_COLOR": "0",
        **home_vars(str(tmp_path)),  # not the real home, and not the demo world's
        "GEMINI_CLI_HOME": str(tmp_path / "gemini"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "GIT_DIR": str(gen_docs.ROOT / ".git"),  # as inside a git hook
        "CLAUDECODE": "1",
        "CI": "true",
    }
    for k, v in ambient.items():
        monkeypatch.setenv(k, v)

    class Later(datetime):  # the fix journal is stamped with the time: it must not show
        @classmethod
        def now(cls, tz=None):
            return datetime(2031, 2, 3, 4, 5, 6, tzinfo=tz or UTC)

    monkeypatch.setattr(fixer, "datetime", Later)
    assert gen_docs.terminal_fragments() == before


def test_fragments_hold_no_machine_paths():
    machine = {str(Path.home()), tempfile.gettempdir(), str(Path(tempfile.gettempdir()).resolve())}
    for name, fragment in gen_docs.terminal_fragments().items():
        text = _text(fragment)
        for path in machine:
            assert path not in text, (name, path)
        assert "fake-home" not in text and "<TMP>" not in text, name


def test_fix_fragment_ends_on_clear():
    text = _text(gen_docs.terminal_fragments()["fix"])
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0] == "agentprint fix  ~ + . · 3 fixes ready"
    assert "Fixing" in lines and "Needs you" in lines
    assert any(line.startswith("  mask    ~/") for line in lines)
    assert any(line.startswith("  env     ~/") for line in lines)
    assert "~/code/my-app/.claude/settings.local.json:4" in text
    assert "Apply 3 fixes? [y/N] y" in lines  # a person answered the question
    assert lines.index("Apply 3 fixes? [y/N] y") < lines.index("3 fixed")
    assert lines[-1].startswith(" CLEAR ") and "BLOCK" not in text


def test_the_question_is_plain_text_for_the_landing_to_type():
    # Terminal.astro finds this line by its exact text and types the answer.
    fragment = gen_docs.terminal_fragments()["fix"]
    assert "\nApply 3 fixes? [y/N] y\n" in fragment


def test_scan_and_fix_tell_one_story():
    """The sweep's BLOCK is the same demo world that fix turns CLEAR: every place fix changes
    is a print the sweep showed."""
    f = gen_docs.terminal_fragments()
    scan, fixed = _text(f["scan"]), _text(f["fix"])
    assert [x for x in scan.splitlines() if x.strip()][-1].startswith(" BLOCK ")
    places = re.findall(r"^  (?:mask|env|ignore|lock) +(\S+)", fixed, re.M)
    assert len(places) == 3
    for place in places:
        assert place in scan, place


def _person_at_a_terminal(monkeypatch, home: Path, project: Path) -> None:
    for name in ("GEMINI_CLI_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    set_home(monkeypatch, str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli, "is_interactive", lambda *a: True)
    monkeypatch.setattr(cli, "is_agent", lambda env: False)


def test_fix_fragment_is_what_agentprint_fix_prints(tmp_path, monkeypatch):
    """The landing's fix block, line for line, is the real command run in ~/code/my-app by a
    person who answers y."""
    home, project = gen_docs.landing_world(tmp_path.resolve())
    _person_at_a_terminal(monkeypatch, home, project)

    result = CliRunner().invoke(cli.app, ["fix"], input="y\n", env={"COLUMNS": "80"})

    assert result.exit_code == 0, result.output
    assert result.output == _text(gen_docs.terminal_fragments()["fix"])


def test_scan_fragment_is_what_agentprint_scan_prints(tmp_path, monkeypatch):
    """Below its header (time and roster are fixed on the landing), the scan block is the real
    sweep of the same world."""
    home, project = gen_docs.landing_world(tmp_path.resolve())
    _person_at_a_terminal(monkeypatch, home, project)

    result = CliRunner().invoke(cli.app, ["scan", str(project)], env={"COLUMNS": "80"})

    assert result.exit_code == 1, result.output
    text = _text(gen_docs.terminal_fragments()["scan"])
    assert text.splitlines()[0] == gen_docs.SCAN_HEADER
    assert result.output.splitlines()[1:] == text.splitlines()[1:]


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


def test_cell_escapes_markdown_significant_characters():
    assert gen_docs._cell("a|b <x> *.env and _k_ `c`") == r"a\|b \<x> \*.env and \_k\_ \`c\`"


def test_check_pages_edit_the_note_not_the_generated_file():
    for doc in gen_docs.check_docs():
        page = gen_docs.check_page(doc)
        assert f"/site/check-notes/{doc.slug}.md\n" in page.split("---\n")[1]
    index = gen_docs.checks_index(gen_docs.check_docs())
    assert "editUrl: false" in index and "label: All checks" in index


def test_one_masked_example_stub_everywhere():
    from pathlib import Path

    docs = Path(__file__).resolve().parents[1] / "site" / "src" / "content" / "docs"
    stubs = set()
    for path in docs.rglob("*.md*"):
        stubs |= set(re.findall(r"sk-a…\(len \d+\)", path.read_text(encoding="utf-8")))
    assert stubs == {"sk-a…(len 105)"}


def test_landing_title_is_not_duplicated():
    from pathlib import Path

    index = Path(__file__).resolve().parents[1] / "site" / "src" / "content" / "docs" / "index.mdx"
    assert "agentprint: the security check" in index.read_text(encoding="utf-8").split("---")[1]


def test_ap009_page_has_title_and_licence_rule():
    doc = [d for d in gen_docs.check_docs() if d.id == "AP009"][0]
    assert doc.title == "Package lets an AI rebuild your source"
    assert "UNLICENSED" in doc.severity and "LicenseRef-" in doc.body
    assert "10 MB" in doc.body and "reverse engineer" in doc.body
