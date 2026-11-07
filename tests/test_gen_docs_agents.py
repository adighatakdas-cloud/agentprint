"""The agents page's table of agents scanned in full with no install yet, drawn from each agent
module's AgentDoc by tools/gen_docs.py."""

from agentprint.agents import ALL_AGENTS
from agentprint.agents.scan_only import SCAN_ONLY_AGENTS
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.roster import scanned
from tools import gen_docs

PAGE = gen_docs.ROOT / "site/src/content/docs/agents.md"


# Full modules that `agentprint install` can arm (their docs rows are written by hand).
ARMABLE = {
    "claude-code", "codex", "cursor", "gemini-cli", "windsurf", "opencode", "copilot", "hermes",
    "antigravity",
}  # fmt: skip


def test_every_full_module_without_an_install_has_a_doc_row():
    full = [a for a in ALL_AGENTS if a not in SCAN_ONLY_AGENTS and a.name not in ARMABLE]
    assert [a.doc for a in full] == gen_docs.scanned_agents()
    assert len(full) == 14
    assert not any(hasattr(a, "doc") for a in ALL_AGENTS if a.name in ARMABLE)
    assert not {a.name for a in full} & set(ALL_INTEGRATIONS)


def test_table_rows_follow_the_status_order(tmp_path):
    labels = [d.label for d in gen_docs.scanned_agents()]
    assert labels == [s.label for s in scanned(tmp_path)]
    table = gen_docs.agents_table(gen_docs.scanned_agents())
    lines = table.splitlines()
    assert len(lines) == 2 + len(labels)
    assert [ln.split(" | ")[0].removeprefix("| ") for ln in lines[2:]] == labels


def test_unverified_paths_are_labelled_and_empty_cells_say_none():
    docs = {d.label: d for d in gen_docs.scanned_agents()}
    table = gen_docs.agents_table(list(docs.values()))
    assert "Docs-unverified (probed)" in table.splitlines()[0]
    for label in ("Factory Droid", "Amp", "Augment (Auggie)", "Cline", "Kilo Code", "Zed"):
        assert docs[label].unverified, label
    row = next(ln for ln in table.splitlines() if ln.startswith("| Kiro |"))
    assert row.endswith("| none |")


def test_pipe_in_a_cell_is_escaped():
    [doc] = gen_docs.scanned_agents()[:1]
    from dataclasses import replace

    table = gen_docs.agents_table([replace(doc, reads="a | b")])
    assert "a \\| b" in table


def test_page_holds_the_generated_table():
    text = PAGE.read_text()
    block = text.split(gen_docs.AGENTS_START)[1].split(gen_docs.AGENTS_END)[0]
    assert block.strip() == gen_docs.agents_table(gen_docs.scanned_agents())
    assert "**Scan-only agents** (4)" in text
    assert f"({len(gen_docs.scanned_agents())} more)" in text
