"""Build a throwaway demo world for agentprint: a fake home folder and a fake npm project.

    python demo/build_demo.py /tmp/agentprint-demo

Creates, under that folder only:

- fake-home/  an AI agent's leftovers: a pasted API key in Claude Code's history (readable by
              other users), a `Bash(*)` permission, and a key written into an MCP server entry;
- my-app/     an npm package whose `.claude/settings.local.json` (holding a key) would ship.

Every key is obviously fake ("FAKEFAKE...") and assembled at runtime, so no key-shaped text is
ever stored in this repository. agentprint always shows them redacted. Nothing outside the
target folder is read or written, and agentprint is never pointed at your real home.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

MARKER = ".agentprint-demo"  # lets a re-run replace a demo folder it made, and nothing else

# Same shapes as tests/fakes.py: real key formats with a FAKE body.
ANTHROPIC = "sk-ant-" + "api03-" + "FAKE" * 23
OPENAI = "sk-" + "proj-" + "FAKE" * 12
GITHUB = "gh" + "p_" + "FAKE" * 9
FAKE_KEYS = (ANTHROPIC, OPENAI, GITHUB)


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _json(data: object) -> str:
    return json.dumps(data, indent=2) + "\n"


def build_home(home: Path) -> None:
    claude = home / ".claude"
    # A key pasted into a prompt, saved in plain text, and readable by every user (0644).
    prompt = {"display": f"deploy with ANTHROPIC_API_KEY={ANTHROPIC}", "project": "/work/my-app"}
    _write(claude / "history.jsonl", json.dumps(prompt) + "\n", mode=0o644)
    # The agent may run any shell command without asking.
    _write(claude / "settings.json", _json({"permissions": {"allow": ["Bash(*)"]}}))
    # A GitHub token written straight into an MCP server entry.
    github = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": GITHUB},
    }
    _write(home / ".claude.json", _json({"mcpServers": {"github": github}}))
    for folder in (home, claude):
        folder.chmod(0o755)


def build_project(project: Path) -> None:
    _write(project / "package.json", _json({"name": "my-app", "version": "1.0.0"}), mode=0o644)
    _write(project / "index.js", "module.exports = () => 'hello';\n", mode=0o644)
    # A personal settings file that npm would publish: no "files" list, no .npmignore.
    local = {"permissions": {"allow": [f"Bash(curl -H 'Authorization: Bearer {OPENAI}' *)"]}}
    _write(project / ".claude" / "settings.local.json", _json(local), mode=0o644)


def build(root: Path) -> tuple[Path, Path]:
    """Create root/fake-home and root/my-app. Refuses a non-empty folder it didn't make."""
    root = root.expanduser().resolve()
    if root == Path.home().resolve() or root in Path.home().resolve().parents:
        raise SystemExit(f"refusing to build the demo in {root}: pick an empty folder")
    if root.exists() and any(root.iterdir()):
        if not (root / MARKER).is_file():
            raise SystemExit(f"refusing to build the demo in {root}: it is not empty")
        for name in ("fake-home", "my-app"):
            shutil.rmtree(root / name, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)  # other users can reach the demo files, so AP008 has something to find
    (root / MARKER).write_text("made by demo/build_demo.py\n", encoding="utf-8")
    home, project = root / "fake-home", root / "my-app"
    build_home(home)
    build_project(project)
    return home, project


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python demo/build_demo.py <empty folder>", file=sys.stderr)
        return 2
    home, project = build(Path(argv[0]))
    print(f"fake home:    {home}")
    print(f"fake project: {project}")
    print("Try (from that folder):")
    print("  agentprint scan --home fake-home my-app")
    print("  agentprint pack my-app --format agent")
    print("  agentprint install --dry-run --home fake-home")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
