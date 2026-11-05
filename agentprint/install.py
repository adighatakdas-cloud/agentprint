"""`agentprint install` / `agentprint uninstall`: wire agentprint into every AI agent found.

For each detected agent this adds, through the agent's own mechanisms: a hook that checks
publishes and pushes (where the agent has hooks), an MCP server entry, and a short instruction
block in its global rules file. It prints agentprint's own added/removed lines, backs each file
up first, is idempotent, and `uninstall` takes out only what agentprint added (restoring the
original bytes when nothing else in the file changed).

Ruling (how agents start agentprint): hooks call a small launcher script,
~/.agentprint/bin/agentprint-hook, which runs `<agentprint> guard`. <agentprint> is the absolute
path of a permanent install if one is on PATH, else `uvx agentprint==<this version>` by absolute
path. Absolute paths survive Gemini CLI's sanitized hook environment and GUI apps that don't
see the shell PATH. Cost if wrong: one extra file on disk, and POSIX only (no Windows hooks).

Launcher exit codes, from each agent's docs (rechecked 2026-11-05):
- Claude Code: 0 = go on (stdout JSON read), 2 = block, others = non-blocking error.
  https://code.claude.com/docs/en/hooks
- Codex: 0 = success, 2 = block (stderr is the reason). https://learn.chatgpt.com/docs/hooks
- Gemini CLI: 0 + JSON, 2 = block, others = warning. https://geminicli.com/docs/hooks/reference
- Cursor: 0 = read JSON (invalid or empty JSON blocks), 2 = block, others fail open.
  https://cursor.com/docs/hooks
- Windsurf Cascade: 2 = block, others proceed (docs.windsurf.com hooks page, archived).
- Devin: 0 = continue, 2 = block, others logged. https://docs.devin.ai/cli/extensibility/hooks/overview
- GitHub Copilot (not wired yet): preToolUse treats any non-zero exit as deny.
  https://docs.github.com/en/copilot/reference/hooks-reference
So: allow = exit 0 with the agent's own allow output; block = the agent's JSON block on exit 0,
or exit 2 for Windsurf (guard exits 3, the launcher turns it into 2). Anything else (uv's own
errors exit 2, a crash, a missing install) = exit 0 plus the agent's allow output and a warning
on stderr: a broken agentprint never blocks, and never answers with Cursor-breaking empty JSON.
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

import yaml

from . import __version__
from .integrations import ALL_INTEGRATIONS
from .integrations.base import (
    BLOCK_EXIT,
    HOOK_MARK,
    MARK,
    BlockEdit,
    Edit,
    FileEdit,
    Integration,
    JsonEdit,
    Launch,
    Unsupported,
    YamlEdit,
)
from .sanitize import clean
from .secrets import find_secrets, redact

STATE_DIR = ".agentprint"
CORE = "agentprint"  # owner of files shared by every agent (the hook launcher)
MAX_DIFF_LINES = 80
Echo = Callable[[str], None]


# ---- how agents start agentprint --------------------------------------------------------------


_THROWAWAY = (
    "/uv/archive-v",  # uvx
    "/.cache/uv/",
    "/pipx/.cache/",  # pipx run: ~/.local/pipx/.cache/<hash>
    "/caches/pipx/",  # pipx run on macOS: ~/Library/Caches/pipx/<hash>
    "/.cache/pipx/",  # pipx run on Linux
    "/tmp/",
    "/temp/",
)


def _ephemeral(path: str) -> bool:
    """True for uvx / pipx run environments and temp dirs, which may vanish at any time."""
    low = path.lower()
    return any(part in low for part in _THROWAWAY)


def resolve_agentprint() -> list[str]:
    exe = shutil.which("agentprint")
    if exe and not _ephemeral(os.path.abspath(exe)):
        return [os.path.abspath(exe)]
    # Pinned: hooks keep running the version that was installed, not whatever is newest.
    return [shutil.which("uvx") or "uvx", f"agentprint=={__version__}"]


def _native_allows() -> str:
    """`case` arms printing each agent's own "allow" answer (only agents that need output)."""
    arms = []
    for name, integration in ALL_INTEGRATIONS.items():
        allow = integration.reply(None).stdout.strip()
        if allow:
            arms.append(f"  {name}) printf '%s\\n' {shlex.quote(allow)} ;;")
    return "\n".join(arms)


def launcher_script(agentprint: list[str]) -> str:
    command = " ".join(shlex.quote(part) for part in agentprint)
    return f"""#!/bin/sh
# {HOOK_MARK}: Written by `agentprint install`; `agentprint uninstall` deletes it.
# Every agent hook agentprint installs runs this script. It starts agentprint's check before a
# package publish or git push, and makes sure a missing or broken agentprint can never block
# your agent: any failure lets the command through (exit 0, the agent's own "allow").
agent=""
previous=""
for arg in "$@"; do
  [ "$previous" = "--agent" ] && agent="$arg"
  previous="$arg"
done
out=$({command} guard "$@")
status=$?
case "$status" in
  0) [ -n "$out" ] && printf '%s\\n' "$out"; exit 0 ;;
  {BLOCK_EXIT}) [ -n "$out" ] && printf '%s\\n' "$out"; exit 2 ;;
esac
echo "agentprint: hook check failed (exit $status); the command was allowed" >&2
case "$agent" in
{_native_allows()}
esac
exit 0
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
        return _real(path).read_bytes().decode("utf-8")  # keeps CRLF line ends
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise Unsupported("it is not UTF-8 text") from exc
    except OSError as exc:
        raise Unsupported(f"it can't be read ({exc.strerror or type(exc).__name__})") from exc


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


def _read_only(path: Path) -> bool:
    try:
        return not _real(path).stat().st_mode & stat.S_IWUSR
    except FileNotFoundError:
        return False


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file that is private (0600) from its first byte, then give it the
    original file's mode (or `mode`, or 0600 for a new file) and move it into place."""
    target = _real(path)
    tmp = target.with_name(f".{target.name}.agentprint-tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()  # a leftover from an interrupted run
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        final = stat.S_IMODE(target.stat().st_mode) if target.exists() else (mode or 0o600)
        os.chmod(tmp, final)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


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


def _json_delta(before: object, after: object) -> tuple[object, object]:
    """(what `after` adds, what it removes) relative to `before`; None when nothing."""
    if isinstance(before, dict) and isinstance(after, dict):
        added, removed = {}, {}
        for key in [*after, *(k for k in before if k not in after)]:  # file order, stable
            if key not in before:
                added[key] = after[key]
            elif key not in after:
                removed[key] = before[key]
            else:
                plus, minus = _json_delta(before[key], after[key])
                if plus is not None:
                    added[key] = plus
                if minus is not None:
                    removed[key] = minus
        return added or None, removed or None
    if isinstance(before, list) and isinstance(after, list):
        added = [item for item in after if item not in before]
        removed = [item for item in before if item not in after]
        return added or None, removed or None
    return (None, None) if before == after else (after, before)


def _data(text: str, edits: list[Edit]) -> object:
    """A JSON or YAML file's data (empty file = {}); ValueError when it can't be read."""
    if any(isinstance(e, YamlEdit) for e in edits):
        try:
            return yaml.safe_load(text or "") or {}
        except yaml.YAMLError as exc:
            raise ValueError(str(exc)) from exc
    if any(isinstance(e, JsonEdit) for e in edits):
        return json.loads(text or "{}")
    raise ValueError("not a data file")


def _render(value: object, edits: list[Edit]) -> str:
    if any(isinstance(e, YamlEdit) for e in edits):
        return yaml.safe_dump(value, sort_keys=False, default_flow_style=False, width=4096)
    return json.dumps(value, indent=2)


def changes(old: str | None, new: str | None, edits: list[Edit]) -> list[str]:
    """The lines agentprint adds (+) or removes (-), and nothing else from the file.

    A JSON file is compared as data, so re-indenting a compact file doesn't print every line
    of it, and none of the user's other values (database URLs, tokens) are ever shown."""
    if any(isinstance(e, JsonEdit | YamlEdit) for e in edits):
        try:
            before, after = _data(old or "", edits), _data(new or "", edits)
        except ValueError:
            return ["    (contents not shown)"]
        added, removed = _json_delta(before, after)
        lines = [f"-{line}" for line in _render(removed, edits).splitlines()] if removed else []
        lines += [f"+{line}" for line in _render(added, edits).splitlines()] if added else []
        json_file = any(isinstance(e, JsonEdit) for e in edits)
        if json_file and old is not None and new is not None and added and not removed:
            lines.append(" (the rest of the file keeps its values; JSON may be re-indented)")
    else:
        lines = [
            line
            for line in difflib.unified_diff(
                (old or "").splitlines(), (new or "").splitlines(), n=0, lineterm=""
            )
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        ]
    shown = [_redact_line(line) for line in lines[:MAX_DIFF_LINES]]
    if len(lines) > MAX_DIFF_LINES:
        shown.append(f"... ({len(lines) - MAX_DIFF_LINES} more lines)")
    return ["    " + line for line in shown]


def _snippet(edits: list[Edit]) -> str:
    """What to add by hand to one file: every agentprint entry for it, in its format."""
    if all(isinstance(e, JsonEdit | YamlEdit) for e in edits):
        sample: dict = {}
        for edit in edits:
            edit.add(sample)
        return _render(sample, edits)
    parts = []
    for edit in edits:
        parts.append(edit.content if isinstance(edit, FileEdit) else edit.apply(None))
    return "\n".join(parts)


def _uses_launcher(path: Path) -> bool:
    try:
        text = _read(path)
    except Unsupported:
        return False
    return text is not None and HOOK_MARK in text


def _would_change(path: Path, edits: list[Edit]) -> bool:
    """True if installing `edits` into `path` would change it (no output, nothing written)."""
    try:
        old = _read(path)
        new = old
        for edit in edits:
            new = edit.apply(new)
    except Exception:  # noqa: BLE001 - a file that can't be edited won't change
        return False
    return new != old


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
            mode=0o700,  # runnable by you only
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
    if len(agentprint) == 2 and agentprint[1].startswith("agentprint=="):
        echo(
            "  (no permanent install found, so hooks use uvx. For faster offline hooks run "
            "`uv tool install agentprint`, then install again.)"
        )
    state = State.load(home)
    changed = 0
    agents = [(i.name, i.label, i.edits(home, env, launch)) for i in found]
    agent_files = [(path, group) for _, _, edits in agents for path, group in _group(edits).items()]
    # The shared launcher goes in when an agent file will use it, or when a hook installed
    # earlier already uses it and the launcher is missing or out of date.
    core_edits = _core_edits(home, agentprint)
    needed = any(_would_change(path, group) for path, group in agent_files) or (
        any(_would_change(edit.path, [edit]) for edit in core_edits)
        and any(_uses_launcher(path) for path, _ in agent_files)
    )
    core = [(CORE, "agentprint", core_edits)] if needed else []
    sections = core + agents
    try:
        for name, label, edits in sections:
            echo(f"\n== {label} ==")
            for path, group in _group(edits).items():
                changed += _install_file(home, path, group, state, dry_run, echo, name)
    finally:  # whatever happens, uninstall must know what was changed
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
    home: Path,
    path: Path,
    edits: list[Edit],
    state: State,
    dry_run: bool,
    echo: Echo,
    agent: str,
) -> int:
    whats = "; ".join(edit.what for edit in edits)
    try:
        old = _read(path)
        new = old
        for edit in edits:
            new = edit.apply(new)
        if new is not None:
            new.encode("utf-8")  # catch unwritable text before anything is shown or written
        if new != old and _read_only(path):
            raise Unsupported("it is read-only (someone locked it on purpose)")
    except Exception as exc:  # noqa: BLE001 - one odd file must never stop the others
        reason = exc if isinstance(exc, Unsupported) else f"of an unexpected {type(exc).__name__}"
        echo(f"  ! {_shown(home, path)}: skipped ({whats}) because {reason}. File left as is.")
        echo("    Add this by hand if you want it:")
        echo("\n".join("      " + line for line in _snippet(edits).splitlines()))
        return 0
    if new == old or new is None:
        echo(f"  {_shown(home, path)}: already set up ({whats})")
        return 0
    echo(f"  {_shown(home, path)}: {whats}")
    for line in changes(old, new, edits):
        echo(line)
    if dry_run:
        return 1
    try:
        _write_with_backup(home, path, old, new, edits, state, agent)
    except OSError as exc:
        echo(f"  ! {_shown(home, path)}: could not write it ({exc}). File left as is.")
        return 0
    return 1


def _write_with_backup(
    home: Path, path: Path, old: str | None, new: str, edits: list[Edit], state: State, agent: str
) -> None:
    key = str(path)
    entry = state.files.get(key) or {"created": old is None, "backup": None}
    if old is not None and not entry.get("backup") and not entry.get("created"):
        backup = _backup_path(home, path)  # taken once, before the first change
        state.dirs.extend(d for d in _mkdirs(backup.parent) if d not in state.dirs)
        backup.write_bytes(_real(path).read_bytes())
        backup.chmod(0o600)
        _private(home / STATE_DIR / "backups")
        entry["backup"] = str(backup)
    state.dirs.extend(d for d in _mkdirs(_real(path).parent) if d not in state.dirs)
    mode = next((e.mode for e in edits if isinstance(e, FileEdit)), None)
    _atomic_write(path, new, mode)
    entry["agent"] = agent
    state.files[key] = entry


def _private(directory: Path) -> None:
    for d in [directory, *[p for p in directory.rglob("*") if p.is_dir()]]:
        d.chmod(stat.S_IRWXU)


# ---- uninstall --------------------------------------------------------------------------------


def _marker_reverts(path: Path) -> list[Edit]:
    """Undo a marked block in a file whose location has moved since install."""
    return [BlockEdit(path, "instructions", "", "md"), BlockEdit(path, "MCP server", "", "toml")]


def uninstall(
    home: Path, env: Mapping[str, str], names: list[str] | None, dry_run: bool, echo: Echo
) -> None:
    launch = Launch(hook=str(home / STATE_DIR / "bin" / HOOK_MARK), mcp=("agentprint", "mcp"))
    chosen = list(names) if names else list(ALL_INTEGRATIONS)
    state = State.load(home)
    echo(f"agentprint uninstall{' (dry run: nothing is written)' if dry_run else ''}")
    grouped: dict[Path, list[Edit]] = {}
    for name in chosen:
        grouped.update(_group(ALL_INTEGRATIONS[name].edits(home, env, launch)))
    for key, entry in state.files.items():  # moved since install, e.g. AGENTS.override.md
        if Path(key) not in grouped and (entry.get("agent") in chosen or not names):
            grouped[Path(key)] = _marker_reverts(Path(key))
    others = [
        key
        for key, entry in state.files.items()
        if entry.get("agent") != CORE and Path(key) not in grouped
    ]
    last = not others  # nothing of another agent left: the launcher and state go too
    if last:
        grouped.update(_group(_core_edits(home, ["agentprint"])))
    changed = 0
    exact = True
    for path, group in grouped.items():
        try:
            result = _uninstall_file(home, path, group, state, dry_run, echo)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the others
            echo(
                f"  ! {_shown(home, path)}: could not undo it ({type(exc).__name__}); left as "
                "is. Run agentprint uninstall again later."
            )
            result = "kept"
        changed += result not in {"same", "kept"}
        exact = exact and result in {"same", "restored", "removed"}
    if dry_run:
        echo(f"\n{changed} file(s) would change. Run without --dry-run to apply.")
        return
    if last and exact:
        shutil.rmtree(home / STATE_DIR / "backups", ignore_errors=True)
        state.path.unlink(missing_ok=True)
        for d in sorted(state.dirs, key=len, reverse=True):
            _rmdir_if_empty(Path(d))
        _rmdir_if_empty(home / STATE_DIR)
    elif state.files or (home / STATE_DIR).exists():
        for d in sorted(state.dirs, key=len, reverse=True):  # e.g. ~/.gemini/config, if empty
            if _rmdir_if_empty(Path(d)):
                state.dirs.remove(d)
        state.save()
        if (home / STATE_DIR / "backups").exists():
            echo(f"Backups kept in {_shown(home, home / STATE_DIR / 'backups')}")
    echo(f"\nDone: {changed} file(s) changed." if changed else "\nNothing to remove.")


def _rmdir_if_empty(directory: Path) -> bool:
    """Remove a folder agentprint created if nothing is left in it. True if it's gone."""
    try:
        directory.rmdir()
    except FileNotFoundError:
        return True
    except OSError:  # not empty
        return False
    return True


def _backup_text(entry: dict | None) -> str | None:
    if not entry or not entry.get("backup"):
        return None
    try:
        return Path(entry["backup"]).read_bytes().decode("utf-8")
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
    """Same content: identical text, or (for JSON / YAML files) the same data."""
    if text == backup:
        return True
    try:
        return _pruned(_data(text, edits)) == _pruned(_data(backup, edits))
    except ValueError:
        return False


def _uninstall_file(
    home: Path, path: Path, edits: list[Edit], state: State, dry_run: bool, echo: Echo
) -> str:
    key = str(path)
    entry = state.files.get(key)
    try:
        current = _read(path)
    except Unsupported:  # not UTF-8 or unreadable: agentprint never wrote it like that
        return "same"
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
    if _read_only(path):
        echo(f"  ! {_shown(home, path)}: left alone because it is read-only. Remove agentprint's "
             "part by hand.")  # fmt: skip
        return "kept"
    created = bool(entry and entry.get("created"))
    backup = _backup_text(entry)
    how = "edited"
    if restored.strip() in ("", "{}") and (created or any(isinstance(e, FileEdit) for e in edits)):
        restored, how = None, "removed"
    elif backup is not None and _same(restored, backup, edits):
        restored, how = backup, "restored"
    echo(f"  {_shown(home, path)}: {'removed' if restored is None else how}")
    for line in changes(current, restored, edits):
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
