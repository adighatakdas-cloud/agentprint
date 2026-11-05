"""install/uninstall robustness from the PR #5 review (items 4, 5, 6, 9). Fake homes only."""

import json
from pathlib import Path

import pytest
import yaml

from agentprint import install

AGENTPRINT = "/opt/agentprint/bin/agentprint"


@pytest.fixture(autouse=True)
def fixed_launch(monkeypatch):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: [AGENTPRINT])


def _install(home, names=None, dry_run=False) -> str:
    out: list[str] = []
    install.install(home, {}, names, dry_run, out.append)
    return "\n".join(out)


def _uninstall(home, names=None) -> str:
    out: list[str] = []
    install.uninstall(home, {}, names, False, out.append)
    return "\n".join(out)


def _hermes(tmp_path, config: str | bytes) -> Path:
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    path = home / ".hermes" / "config.yaml"
    if isinstance(config, bytes):
        path.write_bytes(config)
    else:
        path.write_text(config)
    return home


# ---- 4. odd YAML values and one failing file never stop the rest -------------------------------


def test_dates_in_hermes_hooks_do_not_stop_install(tmp_path):
    config = "hooks:\n  pre_tool_call:\n    - command: x\n      since: 2026-01-01\n"
    home = _hermes(tmp_path, config)
    out = _install(home, ["hermes"])
    assert "skipped" not in out
    data = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
    assert any("agentprint-hook" in h["command"] for h in data["hooks"]["pre_tool_call"])
    _uninstall(home)
    assert (home / ".hermes" / "config.yaml").read_text() == config


def test_one_failing_file_does_not_stop_uninstall_or_lose_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for folder in (".claude", ".codex"):
        (home / folder).mkdir(parents=True)
    _install(home)
    real_read = install._read

    def flaky(path):
        if path.name == "CLAUDE.md":
            raise RuntimeError("disk hiccup")
        return real_read(path)

    monkeypatch.setattr(install, "_read", flaky)
    out = _uninstall(home)
    assert "CLAUDE.md" in out and "could not" in out
    assert not (home / ".codex" / "hooks.json").exists()  # the others were still undone
    state = json.loads((home / ".agentprint" / "state.json").read_text())
    assert str(home / ".claude" / "CLAUDE.md") in state["files"]  # kept for the next try


# ---- 5. Hermes config.yaml: anchors, layout, line endings, comments ----------------------------


@pytest.mark.parametrize(
    "config",
    [
        "base: &b\n  post_tool_call:\n    - command: x\nhooks: *b\n",  # hooks is an alias
        "base: &b\n  post_tool_call: []\nhooks:\n  <<: *b\n",  # hooks merges another mapping
        "srv: &s\n  github:\n    command: gh\nmcp_servers: *s\n",
        "hooks:\n  pre_tool_call: &l\n    - command: x\nother: *l\n",  # the list is anchored
    ],
    ids=["alias", "merge", "servers-alias", "anchored-list"],
)
def test_anchors_and_merges_are_never_written_through(tmp_path, config):
    home = _hermes(tmp_path, config)
    out = _install(home, ["hermes"])
    assert (home / ".hermes" / "config.yaml").read_text() == config
    assert "anchor" in out and "by hand" in out


@pytest.mark.parametrize(
    "config",
    [
        "model:\n    default: x\nhooks:\n    post_tool_call:\n        -   command: y\n",
        "hooks:\n  post_tool_call:\n  - command: y\n    timeout: 5\nmodel: x\n",
        "hooks:\n  post_tool_call:\n    - command: y\nmodel: x   # trailing\n",
    ],
    ids=["four-space", "flush-list", "indented-list"],
)
def test_users_own_lines_are_kept_byte_for_byte(tmp_path, config):
    home = _hermes(tmp_path, config)
    _install(home, ["hermes"])
    after = (home / ".hermes" / "config.yaml").read_text().splitlines()
    remaining = iter(after)
    assert all(line in remaining for line in config.splitlines())  # in order, unchanged
    yaml.safe_load("\n".join(after))
    _uninstall(home)
    assert (home / ".hermes" / "config.yaml").read_text() == config


def test_crlf_and_bom_are_kept(tmp_path):
    raw = "﻿model: x\r\nhooks:\r\n  post_tool_call:\r\n    - command: y\r\n".encode()
    home = _hermes(tmp_path, raw)
    _install(home, ["hermes"])
    after = (home / ".hermes" / "config.yaml").read_bytes()
    assert after.startswith("﻿".encode()) and b"agentprint-hook" in after
    assert after.count(b"\n") == after.count(b"\r\n")  # every line still ends in CRLF
    _uninstall(home)
    assert (home / ".hermes" / "config.yaml").read_bytes() == raw


def test_trailing_comment_stays_after_the_users_hooks(tmp_path):
    config = (
        "hooks:\n  post_tool_call:\n    - command: y\n  # end of my hooks\n"
        "model: x\n"
    )  # fmt: skip
    home = _hermes(tmp_path, config)
    _install(home, ["hermes"])
    text = (home / ".hermes" / "config.yaml").read_text()
    assert text.index("agentprint-hook") < text.index("command: y") < text.index("# end of my")


def test_hooks_key_with_only_comments_counts_as_empty(tmp_path):
    config = "hooks:\n  # add hooks here\nmodel: x\n"
    home = _hermes(tmp_path, config)
    out = _install(home, ["hermes"])
    assert "skipped" not in out
    data = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
    assert data["model"] == "x" and data["hooks"]["pre_tool_call"]


def test_messages_for_yaml_do_not_say_json(tmp_path):
    home = _hermes(tmp_path, "hooks: [not, a, mapping]\n")
    out = _install(home, ["hermes"])
    assert "skipped" in out and "JSON" not in out


# ---- 9. an alias bomb can't hang install -------------------------------------------------------

BOMB = (
    'a: &a ["x","x","x","x","x","x","x","x","x"]\n'
    + "".join(
        f"{n}: &{n} [*{p},*{p},*{p},*{p},*{p},*{p},*{p},*{p},*{p}]\n"
        for p, n in zip("abcdefgh", "bcdefghi", strict=True)
    )
    + "hooks:\n  post_tool_call: [*i]\n"
)


def test_alias_bomb_is_refused_quickly(tmp_path):
    import time

    home = _hermes(tmp_path, BOMB)
    start = time.perf_counter()
    out = _install(home, ["hermes"])
    assert time.perf_counter() - start < 5
    assert "skipped" in out
    assert (home / ".hermes" / "config.yaml").read_text() == BOMB


# ---- 6. Gemini CLI is found by its own files, not by ~/.gemini --------------------------------


def test_antigravity_only_home_gets_no_gemini_cli_files(tmp_path):
    home = tmp_path / "home"
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    out = _install(home)
    assert "Gemini CLI" not in out
    assert not (home / ".gemini" / "settings.json").exists()
    assert not (home / ".gemini" / "GEMINI.md").exists()
    assert (home / ".gemini" / "config" / "hooks.json").exists()  # Antigravity's own


@pytest.mark.parametrize(
    "marker", ["settings.json", "oauth_creds.json", "google_accounts.json", "GEMINI.md", "tmp"]
)
def test_gemini_cli_is_found_by_its_own_files(tmp_path, marker):
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    target = home / ".gemini" / marker
    target.mkdir() if marker == "tmp" else target.write_text("{}\n")
    assert "Gemini CLI" in _install(home, dry_run=True)


# ---- 7. help lists every agent ---------------------------------------------------------------


@pytest.mark.parametrize("command", ["guard", "install", "uninstall"])
def test_help_lists_every_agent(command):
    from typer.testing import CliRunner

    from agentprint.cli import app
    from agentprint.integrations import ALL_INTEGRATIONS

    text = " ".join(CliRunner().invoke(app, [command, "--help"]).output.split())
    for name in ALL_INTEGRATIONS:
        assert name in text, (command, name)
