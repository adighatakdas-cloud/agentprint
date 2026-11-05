"""`agentprint install` / `agentprint uninstall`: wire agentprint into every AI agent found.

For each detected agent this adds, through the agent's own mechanisms: a hook that checks
publishes and pushes (where the agent has hooks), an MCP server entry, and a short instruction
block in its global rules file. It prints a diff of every change, backs each file up first, is
idempotent, and `uninstall` puts every file back byte for byte when it hasn't changed since.

Ruling (how agents start agentprint): hooks call a small launcher script,
~/.agentprint/bin/agentprint-hook, which runs `<agentprint> guard` and turns every failure into
"allow" (exit 1). <agentprint> is the absolute path of a permanent install if one is on PATH,
else `uvx agentprint` by absolute path. Why: uv exits 2 on its own errors, and exit 2 means
"block" to Claude Code, Codex, Gemini CLI and Cursor; without the launcher a cold uv cache or
an offline laptop could block every shell command. Absolute paths also survive Gemini CLI's
sanitized hook environment and GUI apps that don't see the shell PATH. Cost if wrong: one extra
file on disk, and POSIX only (no Windows hooks yet).
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import shlex
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .integrations import ALL_INTEGRATIONS
from .integrations.base import (
    BLOCK_EXIT,
    HOOK_MARK,
    MARK,
    Edit,
    FileEdit,
    Integration,
    JsonEdit,
    Launch,
    Unsupported,
)
from .sanitize import clean
from .secrets import find_secrets, redact

STATE_DIR = ".agentprint"
MAX_DIFF_LINES = 80
Echo = Callable[[str], None]


# ---- how agents start agentprint --------------------------------------------------------------


def _ephemeral(path: str) -> bool:
    """True for uvx's throwaway environments and temp dirs, which may vanish."""
    low = path.lower()
    return any(part in low for part in ("/uv/archive-v", "/.cache/uv/", "/tmp/", "/temp/"))


def resolve_agentprint() -> list[str]:
    exe = shutil.which("agentprint")
    if exe and not _ephemeral(os.path.abspath(exe)):
        return [os.path.abspath(exe)]
    return [shutil.which("uvx") or "uvx", "agentprint"]


def launcher_script(agentprint: list[str]) -> str:
    command = " ".join(shlex.quote(part) for part in agentprint)
    return f"""#!/bin/sh
# {HOOK_MARK}: Written by `agentprint install`; `agentprint uninstall` deletes it.
# Every agent hook agentprint installs runs this script. It starts agentprint's check before a
# package publish or git push, and makes sure a missing or broken agentprint can never block
# your agent: any failure lets the command through.
{command} guard "$@"
status=$?
case "$status" in
  0) exit 0 ;;
  {BLOCK_EXIT}) exit 2 ;;
  *) echo "agentprint: hook check failed (exit $status); the command was allowed" >&2; exit 1 ;;
esac
"""


# ---- state, backups, safe writes --------------------------------------------------------------


@dataclass
class State:
    path: Path
    files: dict[str, dict] = field(default_factory=dict)
    dirs: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, home: Path) -> State:
        path = home / STATE_DIR / "state.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(path, dict(data.get("files", {})), list(data.get("dirs", [])))
        except (OSError, ValueError):
            return cls(path)

    def save(self) -> None:
        data = {"version": 1, "files": self.files, "dirs": self.dirs}
        self.dirs.extend(d for d in _mkdirs(self.path.parent) if d not in self.dirs)
        _atomic_write(self.path, json.dumps(data, indent=2) + "\n", 0o600)


def _real(path: Path) -> Path:
    """Edit through symlinks (dotfile setups) instead of replacing the link."""
    return path.resolve() if path.is_symlink() else path


def _read(path: Path) -> str | None:
    try:
        return _real(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _mkdirs(directory: Path) -> list[str]:
    created: list[str] = []
    missing = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for d in reversed(missing):
        d.mkdir()
        created.append(str(d))
    return created


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    target = _real(path)
    tmp = target.with_name(f".{target.name}.agentprint-tmp")
    tmp.write_bytes(text.encode("utf-8"))
    if target.exists():
        shutil.copymode(target, tmp)
    elif mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, target)


def _backup_path(home: Path, path: Path) -> Path:
    try:
        rel = path.relative_to(home)
    except ValueError:
        rel = Path("abs") / str(path).lstrip("/")
    return home / STATE_DIR / "backups" / rel


# ---- output -----------------------------------------------------------------------------------


def _shown(home: Path, path: Path) -> str:
    text = str(path)
    return "~" + text[len(str(home)) :] if text.startswith(str(home) + os.sep) else text


def _redact_line(line: str) -> str:
    for match in reversed(find_secrets(line)):
        line = line[: match.start] + redact(match.value) + line[match.end :]
    return clean(line)


def diff(old: str | None, new: str | None, label: str) -> list[str]:
    lines = list(
        difflib.unified_diff(
            (old or "").splitlines(),
            (new or "").splitlines(),
            fromfile=f"a/{label}" if old is not None else "/dev/null",
            tofile=f"b/{label}" if new is not None else "/dev/null",
            n=2,
            lineterm="",
        )
    )
    shown = [_redact_line(line) for line in lines[:MAX_DIFF_LINES]]
    if len(lines) > MAX_DIFF_LINES:
        shown.append(f"... ({len(lines) - MAX_DIFF_LINES} more diff lines)")
    return ["    " + line for line in shown]


def _snippet(edit: Edit) -> str:
    if isinstance(edit, JsonEdit):
        sample: dict = {}
        edit.add(sample)
        return json.dumps(sample, indent=2)
    if isinstance(edit, FileEdit):
        return edit.content
    return edit.apply(None)


# ---- install ----------------------------------------------------------------------------------


def _group(edits: list[Edit]) -> dict[Path, list[Edit]]:
    grouped: dict[Path, list[Edit]] = {}
    for edit in edits:
        grouped.setdefault(edit.path, []).append(edit)
    return grouped


def _core_edits(home: Path, agentprint: list[str]) -> list[Edit]:
    return [
        FileEdit(
            home / STATE_DIR / "bin" / HOOK_MARK,
            "hook launcher (lets agents run agentprint safely)",
            launcher_script(agentprint),
            mode=0o755,
        )
    ]


def select(names: list[str] | None, home: Path, env: Mapping[str, str]) -> list[Integration]:
    chosen = [ALL_INTEGRATIONS[n] for n in names] if names else list(ALL_INTEGRATIONS.values())
    return [i for i in chosen if i.installed(home, env)]


def install(
    home: Path, env: Mapping[str, str], names: list[str] | None, dry_run: bool, echo: Echo
) -> None:
    agentprint = resolve_agentprint()
    launch = Launch(hook=str(home / STATE_DIR / "bin" / HOOK_MARK), mcp=(*agentprint, "mcp"))
    found = select(names, home, env)
    echo(f"agentprint install{' (dry run: nothing is written)' if dry_run else ''}")
    if not found:
        echo("No supported AI agent found under " + str(home) + ". Nothing to do.")
        return
    echo("Agents found: " + ", ".join(i.label for i in found))
    echo("Agents will start agentprint with: " + " ".join(agentprint))
    if agentprint[-1] == "agentprint" and len(agentprint) == 2:
        echo(
            "  (no permanent install found, so hooks use uvx. For faster offline hooks run "
            "`uv tool install agentprint`, then install again.)"
        )
    state = State.load(home)
    changed = 0
    sections: list[tuple[str, list[Edit]]] = [("agentprint", _core_edits(home, agentprint))]
    sections += [(i.label, i.edits(home, env, launch)) for i in found]
    for label, edits in sections:
        echo(f"\n== {label} ==")
        for path, group in _group(edits).items():
            changed += _install_file(home, path, group, state, dry_run, echo)
    if not dry_run and changed:
        state.save()
    echo("")
    if changed == 0:
        echo("Everything is already set up. Nothing changed.")
    elif dry_run:
        echo(f"{changed} file(s) would change. Run without --dry-run to apply.")
    else:
        echo(f"Done: {changed} file(s) changed. Backups: {_shown(home, home / STATE_DIR)}")
        echo("Undo everything with: agentprint uninstall")
    for integration in found:
        for note in integration.notes:
            echo(f"Note for {integration.label}: {note}")


def _install_file(
    home: Path, path: Path, edits: list[Edit], state: State, dry_run: bool, echo: Echo
) -> int:
    old = _read(path)
    new = old
    whats = []
    for edit in edits:
        try:
            new = edit.apply(new)
            whats.append(edit.what)
        except Unsupported as exc:
            echo(f"  ! {_shown(home, path)}: skipped ({edit.what}) because {exc}.")
            echo("    Add this by hand if you want it:")
            echo("\n".join("      " + line for line in _snippet(edit).splitlines()))
    if new == old or new is None:
        if whats:
            echo(f"  {_shown(home, path)}: already set up ({'; '.join(whats)})")
        return 0
    echo(f"  {_shown(home, path)}: {'; '.join(whats)}")
    for line in diff(old, new, _shown(home, path)):
        echo(line)
    if dry_run:
        return 1
    key = str(path)
    if key not in state.files:  # keep the backup from before the first install
        entry: dict = {"created": old is None, "backup": None}
        if old is not None:
            backup = _backup_path(home, path)
            state.dirs.extend(d for d in _mkdirs(backup.parent) if d not in state.dirs)
            backup.write_bytes(_real(path).read_bytes())
            backup.chmod(0o600)
            _private(home / STATE_DIR / "backups")
            entry["backup"] = str(backup)
        state.files[key] = entry
    state.dirs.extend(d for d in _mkdirs(_real(path).parent) if d not in state.dirs)
    mode = next((e.mode for e in edits if isinstance(e, FileEdit)), None)
    _atomic_write(path, new, mode)
    return 1


def _private(directory: Path) -> None:
    for d in [directory, *[p for p in directory.rglob("*") if p.is_dir()]]:
        d.chmod(stat.S_IRWXU)


# ---- uninstall --------------------------------------------------------------------------------


def uninstall(
    home: Path, env: Mapping[str, str], names: list[str] | None, dry_run: bool, echo: Echo
) -> None:
    launch = Launch(hook=str(home / STATE_DIR / "bin" / HOOK_MARK), mcp=("agentprint", "mcp"))
    chosen = [ALL_INTEGRATIONS[n] for n in names] if names else list(ALL_INTEGRATIONS.values())
    state = State.load(home)
    echo(f"agentprint uninstall{' (dry run: nothing is written)' if dry_run else ''}")
    edits: list[Edit] = [e for i in chosen for e in i.edits(home, env, launch)]
    if not names:
        edits += _core_edits(home, ["agentprint"])
    grouped = _group(edits)
    for key in state.files:  # files recorded at install whose location has since moved
        grouped.setdefault(Path(key), [])
    changed = 0
    exact = True
    for path, group in grouped.items():
        result = _uninstall_file(home, path, group, state, dry_run, echo)
        changed += result != "same"
        exact = exact and result in {"same", "restored", "removed"}
    if dry_run:
        echo(f"\n{changed} file(s) would change. Run without --dry-run to apply.")
        return
    if not names and exact:
        shutil.rmtree(home / STATE_DIR / "backups", ignore_errors=True)
        state.path.unlink(missing_ok=True)
        for d in sorted(state.dirs, key=len, reverse=True):
            _rmdir_if_empty(Path(d))
        _rmdir_if_empty(home / STATE_DIR)
    else:
        state.save()
        echo(f"Backups kept in {_shown(home, home / STATE_DIR / 'backups')}")
    echo(f"\nDone: {changed} file(s) changed." if changed else "\nNothing to remove.")


def _rmdir_if_empty(directory: Path) -> None:
    with contextlib.suppress(OSError):  # not empty, or already gone
        directory.rmdir()


def _backup_text(entry: dict | None) -> str | None:
    if not entry or not entry.get("backup"):
        return None
    try:
        return Path(entry["backup"]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _pruned(value: object) -> object:
    """JSON data without empty objects/lists, which removing an entry can leave or prune."""
    if isinstance(value, dict):
        items = {k: _pruned(v) for k, v in value.items()}
        return {k: v for k, v in items.items() if v not in ({}, [])}
    if isinstance(value, list):
        return [_pruned(v) for v in value]
    return value


def _same(text: str, backup: str, edits: list[Edit]) -> bool:
    """Same content: identical text, or (for JSON files) the same data."""
    if text == backup:
        return True
    if not any(isinstance(e, JsonEdit) for e in edits):
        return False
    try:
        return _pruned(json.loads(text or "{}")) == _pruned(json.loads(backup or "{}"))
    except ValueError:
        return False


def _uninstall_file(
    home: Path, path: Path, edits: list[Edit], state: State, dry_run: bool, echo: Echo
) -> str:
    key = str(path)
    entry = state.files.get(key)
    current = _read(path)
    if current is None or (entry is None and MARK not in current):
        if not dry_run:
            state.files.pop(key, None)
        return "same"
    # Only ever take agentprint's own edits out. The backup is used solely to get the original
    # bytes back (formatting included) when what remains is the same as the backup.
    restored: str | None = current
    try:
        for edit in reversed(edits):
            restored = edit.revert(restored)
    except Unsupported as exc:
        echo(f"  ! {_shown(home, path)}: left alone because {exc}. Remove agentprint by hand.")
        return "kept"
    if restored == current:
        return "same"
    created = bool(entry and entry.get("created"))
    backup = _backup_text(entry)
    how = "edited"
    if restored.strip() in ("", "{}") and (created or any(isinstance(e, FileEdit) for e in edits)):
        restored, how = None, "removed"
    elif backup is not None and _same(restored, backup, edits):
        restored, how = backup, "restored"
    echo(f"  {_shown(home, path)}: {'removed' if restored is None else how}")
    for line in diff(current, restored, _shown(home, path)):
        echo(line)
    if dry_run:
        return how
    if restored is None:
        _real(path).unlink()
    elif how == "restored":
        _real(path).write_bytes(Path(entry["backup"]).read_bytes())  # exact original bytes
    else:
        _atomic_write(path, restored)
    state.files.pop(key, None)
    return how
