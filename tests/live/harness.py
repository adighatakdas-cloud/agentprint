"""Shared pieces for live agent tests: fake tools on PATH, projects, a clean environment.

Everything lives under one temporary folder that doubles as the agent's HOME. Nothing reads
or writes the real home, and the agent never sees the real environment (no real API keys).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from tests import fakes

DUMMY_KEY = "sk-test-dummy"
PUBLISH = "npm publish"
PUSH = "git push"

NPM_SHIM = """#!{python}
# Fake npm for live tests: `npm pack --dry-run --json` lists the folder (as agentprint's
# guard asks for), `npm publish` only leaves a marker file. Nothing is ever uploaded.
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if "pack" in args:
    root = Path.cwd()
    meta = json.loads((root / "package.json").read_text())
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                   if p.is_file() and not {{"node_modules", ".git"}} & set(p.parts))
    name = f"{{meta['name']}}-{{meta['version']}}.tgz"
    print(json.dumps([{{"filename": name, "files": [{{"path": f}} for f in files]}}]))
elif "publish" in args:
    with open(os.path.join({markers!r}, "PUBLISHED"), "a") as fh:
        fh.write(os.getcwd() + "\\n")
    print("+ published (fake npm)")
"""

GIT_SHIM = """#!{python}
# Fake git for live tests: `git push` only leaves a marker file; everything else is real git.
import os, sys
args = sys.argv[1:]
i = 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if args[i] in ("-C", "-c") else 1
if i < len(args) and args[i] == "push":
    with open(os.path.join({markers!r}, "PUSHED"), "a") as fh:
        fh.write(os.getcwd() + "\\n")
    print("pushed (fake git)")
    sys.exit(0)
os.execv({git!r}, [{git!r}, *args])
"""


def make_shims(bin_dir: Path, markers: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    markers.mkdir(parents=True, exist_ok=True)
    git = shutil.which("git") or "/usr/bin/git"
    for name, text in (("npm", NPM_SHIM), ("git", GIT_SHIM)):
        path = bin_dir / name
        path.write_text(text.format(python=sys.executable, markers=str(markers), git=git))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


LEAK = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}})


def package(root: Path, leaky: bool) -> Path:
    _write(root / "package.json", json.dumps({"name": "live-demo", "version": "1.0.0"}))
    _write(root / "index.js", "module.exports = 1;\n")
    if leaky:
        _write(root / ".claude" / "settings.local.json", LEAK)
    return root


def repo(root: Path, leaky: bool) -> Path:
    _write(root / "README.md", "demo\n")
    if leaky:
        _write(root / ".claude" / "settings.local.json", LEAK)
    git = shutil.which("git") or "/usr/bin/git"
    env = {**_git_env(root.parent), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run([git, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                       cwd=root, check=True, env=env)  # fmt: skip
    return root


def _git_env(home: Path) -> dict[str, str]:
    """Keep the machine's own git config and global ignore file out of the test repos."""
    return {
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig-live"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
    }


def agent_env(home: Path, bin_dir: Path, tool_dirs: list[Path], **extra: str) -> dict[str, str]:
    """A from-scratch environment: fake HOME, shims first on PATH, nothing inherited that could
    carry a real key or point at the real home."""
    node = shutil.which("node")
    path = [str(bin_dir), *map(str, tool_dirs)]
    if node:
        path.append(str(Path(node).parent))
    path += ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env = {
        **_git_env(home),
        "PATH": os.pathsep.join(path),
        "TMPDIR": str(home / "tmp"),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CI": "1",
        "ZDOTDIR": str(home),
        # Belt and braces: even if an agent's login shell finds the real npm, `npm publish`
        # is only a dry run. Nothing is ever published. (Not also an unreachable registry:
        # OpenCode's own package installs would then hang for a minute per run.)
        "npm_config_dry_run": "true",
    }
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    _login_shell_files(home, bin_dir)
    env.update(extra)
    return env


def _login_shell_files(home: Path, bin_dir: Path) -> None:
    """Some agents run commands in a login shell (`zsh -lc`, `bash -lc`), which rebuilds PATH
    from the system profile. These files in the fake HOME put the fake npm/git first again."""
    line = f'export PATH="{bin_dir}:$PATH"\n'
    for name in (".zprofile", ".zshrc", ".zshenv", ".bash_profile", ".bashrc", ".profile"):
        (home / name).write_text(line)
