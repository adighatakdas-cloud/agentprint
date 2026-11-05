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
