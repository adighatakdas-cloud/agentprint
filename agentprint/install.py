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
see the shell PATH. Cost if wrong: one extra file on disk.

Ruling (Windows): the launcher is ~/.agentprint/bin/agentprint-hook.ps1, run by Windows
PowerShell (`powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass`), with the same
exit-code rules. Hooks go only into agents whose docs say how a hook runs on Windows (Claude Code's
`shell: "powershell"`, Copilot's `powershell` field, Codex's `commandWindows`, Cursor's
PowerShell runner: `Integration.windows_hooks`); the others get the MCP server and the note,
and install says which is which. The hook input goes to agentprint on stdin untouched: the
launcher never reads `$input` (PowerShell then leaves stdin alone) and agentprint inherits it.
Cost if wrong: an agent that runs Windows hooks some other way gets no check, only the note.

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

import difflib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import __version__, platform, powershell
from .integrations import ALL_INTEGRATIONS
from .integrations.base import (
    BLOCK_EXIT,
    HOOK_MARK,
    MARK,
    POWERSHELL,
    WINDOWS_LAUNCHER,
    BlockEdit,
    Edit,
    FileEdit,
    Integration,
    JsonEdit,
    Launch,
    Unsupported,
    YamlEdit,
    expanded_size,
)
from .safewrite import atomic_write as _atomic_write
from .safewrite import private_dir as _private
from .safewrite import real as _real
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
    "/uv/cache/archive-v",  # uvx on Windows: %LOCALAPPDATA%\\uv\\cache\\archive-v0\\...
    "/appdata/local/temp/",  # %TEMP% on Windows
)


def _ephemeral(path: str) -> bool:
    """True for uvx / pipx run environments and temp dirs, which may vanish at any time."""
    low = str(path).replace("\\", "/").lower()  # Windows paths too, POSIX ones unchanged
    return any(part in low for part in _THROWAWAY)


def pinned_uvx() -> list[str]:
    # Pinned: hooks keep running the version that was installed, not whatever is newest.
    return [shutil.which("uvx") or "uvx", f"agentprint=={__version__}"]


def resolve_agentprint() -> list[str]:
    exe = shutil.which("agentprint")
    if exe and not _ephemeral(os.path.abspath(exe)):
        return [os.path.abspath(exe)]
    return pinned_uvx()


STRICT_SINCE = (0, 1, 1)  # the first agentprint whose guard honours --strict
_VERSION = re.compile(r"\bagentprint (\d+)\.(\d+)\.(\d+)")


def reported_version(agentprint: list[str]) -> tuple[int, ...] | None:
    """The version `<agentprint> --version` prints, or None if it cannot be run or read."""
    try:
        done = subprocess.run(
            [*agentprint, "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            **platform.subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    found = _VERSION.search(done.stdout or "")
    return tuple(int(part) for part in found.groups()) if found and done.returncode == 0 else None


def ignores_strict(agentprint: list[str]) -> str | None:
    """Why the agentprint hooks would run cannot be trusted with --strict, or None if it can.
    An older guard accepts --strict and ignores it, so its version is checked first."""
    if agentprint[1:] == [f"agentprint=={__version__}"]:
        return None  # uvx runs this very version
    version = reported_version(agentprint)
    if version is None:
        return "did not report its version"
    if version < STRICT_SINCE:
        return f"is version {'.'.join(map(str, version))}, which ignores strict mode"
    return None


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


def _file_text(text: str) -> str:
    return powershell.literal(text, powershell.IN_FILE)


def windows_launcher_script(agentprint: list[str]) -> str:
    """agentprint-hook.ps1: launcher_script's rules in Windows PowerShell 5.1 (and 7). ASCII
    only, since 5.1 reads a .ps1 without a byte-order mark in the ANSI code page."""
    allows = []
    for name, integration in ALL_INTEGRATIONS.items():
        allow = integration.reply(None).stdout.strip()
        if allow:
            allows.append(f"            {_file_text(name)} {{ $allow = {_file_text(allow)} }}")
    fixed = subprocess.list2cmdline([*agentprint[1:], "guard"])
    lines = [
        f"# {HOOK_MARK}: Written by `agentprint install`; `agentprint uninstall` deletes it.",
        "# Every agent hook agentprint installs on Windows runs this script with Windows",
        f"# PowerShell ({POWERSHELL}).",
        "# It starts agentprint's check before a package publish or git push, and makes sure a",
        "# missing or broken agentprint can never block your agent: any failure lets the command",
        "# through (exit 0, the agent's own allow). It never exits 1. The hook input on stdin goes",
        "# to agentprint as it is (this script never reads it, so PowerShell leaves it alone).",
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'  # no progress records on stderr",
        "$agent = ''",
        "$strict = $false",
        "function Send-Bytes([byte[]]$bytes) {",
        "    if ($null -ne $bytes -and $bytes.Length -gt 0) {",
        "        $stdout = [Console]::OpenStandardOutput()",
        "        $stdout.Write($bytes, 0, $bytes.Length)",
        "        $stdout.Flush()",
        "    }",
        "}",
        "function Exit-Allowed([string]$why) {",
        "    try {",
        "        [Console]::Error.WriteLine('agentprint: hook check failed (' + $why + '); ' +",
        "            'the command was allowed')",
        "        $allow = ''",
        "        switch ($agent) {",
        *allows,
        "        }",
        '        if ($allow) { Send-Bytes ([Text.Encoding]::UTF8.GetBytes($allow + "`n")) }',
        "    } catch { }",
        "    exit 0",
        "}",
        "trap { Exit-Allowed ('launcher error: ' + $_.Exception.GetType().Name) }",
        "# --agent <name> and --strict, however this PowerShell passed them on (-File can turn",
        "# --agent into a named parameter): agentprint gets them in one known form.",
        "for ($i = 0; $i -lt $args.Count; $i++) {",
        "    $word = [string]$args[$i]",
        "    if ($word -match '^-+agent[:=](.+)$') { $agent = $Matches[1] }",
        "    elseif ($word -match '^-+agent:?$' -and $i + 1 -lt $args.Count) {",
        "        $i++",
        "        $agent = [string]$args[$i]",
        "    }",
        "    elseif ($word -match '^-+strict$') { $strict = $true }",
        "}",
        "if ($agent -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]*$') { Exit-Allowed 'no agent name' }",
        "$env:PYTHONUTF8 = '1'",
        "$start = New-Object System.Diagnostics.ProcessStartInfo",
        f"$start.FileName = {_file_text(agentprint[0])}",
        f"$start.Arguments = {_file_text(fixed)} + ' --agent ' + $agent",
        "if ($strict) { $start.Arguments += ' --strict' }",
        "$start.UseShellExecute = $false",
        "$start.RedirectStandardOutput = $true  # stdin and stderr: this script's own",
        "$start.CreateNoWindow = $true",
        "try { $child = [System.Diagnostics.Process]::Start($start) }",
        "catch { Exit-Allowed 'agentprint did not start' }",
        "$buffer = New-Object System.IO.MemoryStream",
        "$child.StandardOutput.BaseStream.CopyTo($buffer)",
        "$child.WaitForExit()",
        "$status = $child.ExitCode",
        "if ($status -eq 0) { Send-Bytes $buffer.ToArray(); exit 0 }",
        f"if ($status -eq {BLOCK_EXIT}) {{ Send-Bytes $buffer.ToArray(); exit 2 }}",
        "Exit-Allowed ('exit ' + $status)",
    ]
    return "\n".join(lines) + "\n"


# ---- state, backups, safe writes --------------------------------------------------------------

# _atomic_write, _real and _private come from safewrite (shared with `agentprint fix`).


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


def _read(path: Path) -> str | None:
    try:
        return _real(path).read_bytes().decode("utf-8")  # keeps CRLF line ends
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise Unsupported("it is not UTF-8 text") from exc
    except OSError as exc:
        raise Unsupported(f"it can't be read ({exc.strerror or type(exc).__name__})") from exc


def _uses_crlf(text: str | None) -> bool:
    """True when every line end in the text is CRLF (a file saved on Windows)."""
    return bool(text) and "\r\n" in text and text.count("\n") == text.count("\r\n")


def _through_edits(text: str | None, edits: list[Edit], *, revert: bool = False) -> str | None:
    """Apply (or take out) the edits on LF text and give the result the file's own line ends,
    so a CRLF file stays CRLF; unchanged text comes back as the very same string."""
    crlf = _uses_crlf(text)
    work = text.replace("\r\n", "\n") if crlf and text is not None else text
    new = work
    for edit in reversed(edits) if revert else edits:
        new = edit.revert(new or "") if revert else edit.apply(new)
    if new == work:
        return text
    return new.replace("\n", "\r\n") if crlf and new is not None else new


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


def _backup_path(home: Path, path: Path) -> Path:
    try:
        rel = path.relative_to(home)
    except ValueError:
        rel = Path("abs", *platform.drive_safe_parts(path))  # drive kept: C/x/y, UNC/host/share
    return home / STATE_DIR / "backups" / rel


# ---- output -----------------------------------------------------------------------------------


def _shown(home: Path, path: Path) -> str:
    text = str(path)
    if not text.startswith(str(home) + os.sep):
        return text
    return "~" + text[len(str(home)) :].replace(os.sep, "/")  # the same ~/.x on every OS


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
    """A JSON or YAML file's data (empty file = {}); ValueError when it can't be read, or when
    its anchors would expand it past MAX_EXPANDED values (checked before anything walks it)."""
    if any(isinstance(e, YamlEdit) for e in edits):
        try:
            data = yaml.safe_load((text or "").lstrip("\ufeff")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(str(exc)) from exc
    elif any(isinstance(e, JsonEdit) for e in edits):
        data = json.loads(text.removeprefix("\ufeff") if text else "{}")
    else:
        raise ValueError("not a data file")
    expanded_size(data)
    return data


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
            if isinstance(edit, JsonEdit | YamlEdit):
                edit.add(sample)
        return _render(json.loads(json.dumps(sample, default=str)), edits)  # plain types
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
        new = _through_edits(old, edits)
    except Exception:  # noqa: BLE001 - a file that can't be edited won't change
        return False
    return new != old


# ---- install ----------------------------------------------------------------------------------


def _hook_free(integration: Integration, edits: list[Edit]) -> list[Edit]:
    """On Windows an agent gets a hook only if its docs say how one runs there; the others get
    only the MCP server and the note."""
    if not platform.is_windows() or integration.windows_hooks:
        return edits
    return [e for e in edits if not e.what.startswith("hook")]


def windows_hooks_line(found: list[Integration]) -> str:
    """What install puts in on Windows, agent by agent: hooks, or only the MCP server and the
    note."""
    hooked = [i.label for i in found if i.windows_hooks] or ["none of these agents"]
    rest = [i.label for i in found if not i.windows_hooks]
    line = f"Hooks on Windows: {', '.join(hooked)}."
    if rest:
        line += f" Only the MCP server and the note (no Windows hooks yet): {', '.join(rest)}."
    return line


def _launcher_path(home: Path, windows: bool) -> Path:
    return home / STATE_DIR / "bin" / (WINDOWS_LAUNCHER if windows else HOOK_MARK)


def _group(edits: list[Edit]) -> dict[Path, list[Edit]]:
    grouped: dict[Path, list[Edit]] = {}
    for edit in edits:
        grouped.setdefault(edit.path, []).append(edit)
    return grouped


def _core_edits(home: Path, agentprint: list[str], windows: bool = False) -> list[Edit]:
    script = windows_launcher_script(agentprint) if windows else launcher_script(agentprint)
    return [
        FileEdit(
            _launcher_path(home, windows),
            "hook launcher (lets agents run agentprint safely)",
            script,
            mode=0o700,  # runnable by you only
        )
    ]


def select(names: list[str] | None, home: Path, env: Mapping[str, str]) -> list[Integration]:
    chosen = [ALL_INTEGRATIONS[n] for n in names] if names else list(ALL_INTEGRATIONS.values())
    return [i for i in chosen if i.installed(home, env)]


APPLY_HINT = "Run without --dry-run to apply."


def install(
    home: Path,
    env: Mapping[str, str],
    names: list[str] | None,
    dry_run: bool,
    echo: Echo,
    *,
    apply_hint: str | None = APPLY_HINT,
    strict: bool = False,
) -> int:
    """Arm the agents found; returns how many files changed (or would, in a dry run).

    apply_hint ends a dry run's count line; the menu, which asks y/N itself, passes None."""
    windows = platform.is_windows()  # hooks only for agents that document Windows hooks
    agentprint = resolve_agentprint()
    found = select(names, home, env)
    hooked = [i for i in found if i.windows_hooks] if windows else found
    # Strict hooks must run an agentprint that honours --strict: an older permanent install is
    # passed over for uvx running this version, so the promise printed below holds.
    stale = (agentprint[0], ignores_strict(agentprint)) if strict and hooked else None
    if stale and stale[1]:
        agentprint = pinned_uvx()
    launch = Launch(
        hook=str(_launcher_path(home, windows)),
        mcp=(*agentprint, "mcp"),
        strict=strict,
        windows=windows,
    )
    echo(f"agentprint install{' (dry run: nothing is written)' if dry_run else ''}")
    if not found:
        echo("No supported AI agent found under " + str(home) + ". Nothing to do.")
        return 0
    echo("Agents found: " + ", ".join(i.label for i in found))
    echo("Agents will start agentprint with: " + " ".join(agentprint))
    if not hooked:
        pass  # no hooks here: nothing below about how hooks start agentprint applies
    elif stale and stale[1]:
        echo(
            f"  (the agentprint at {stale[0]} {stale[1]}, so hooks use uvx with agentprint "
            f"{__version__} instead. Upgrade it with `uv tool upgrade agentprint`, then install "
            "again.)"
        )
    elif len(agentprint) == 2 and agentprint[1].startswith("agentprint=="):
        echo(
            "  (no permanent install found, so hooks use uvx. For faster offline hooks run "
            "`uv tool install agentprint`, then install again.)"
        )
    if windows:
        echo(windows_hooks_line(found))
    if strict and hooked:
        echo("Strict mode: a publish or push agentprint cannot check will be blocked.")
    state = State.load(home)
    changed = 0
    agents = [(i.name, i.label, _hook_free(i, i.edits(home, env, launch))) for i in found]
    agent_files = [(path, group) for _, _, edits in agents for path, group in _group(edits).items()]
    # The shared launcher goes in when an agent file will use it, or when a hook installed
    # earlier already uses it and the launcher is missing or out of date.
    core_edits = _core_edits(home, agentprint, windows) if hooked else []
    needed = any(_would_change(path, group) for path, group in agent_files) or (
        any(_would_change(edit.path, [edit]) for edit in core_edits)
        and any(_uses_launcher(path) for path, _ in agent_files)
    )
    core = [(CORE, "agentprint", core_edits)] if needed and core_edits else []
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
        echo(f"{changed} file(s) would change." + (f" {apply_hint}" if apply_hint else ""))
    else:
        echo(f"Done: {changed} file(s) changed. Backups: {_shown(home, home / STATE_DIR)}")
        echo("Undo everything with: agentprint uninstall")
    for integration in hooked:  # the notes are about trusting hooks
        for note in integration.notes:
            echo(f"Note for {integration.label}: {note}")
    return changed


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
        new = _through_edits(old, edits)
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


# ---- uninstall --------------------------------------------------------------------------------


def _marker_reverts(path: Path) -> list[Edit]:
    """Undo a marked block in a file whose location has moved since install."""
    return [BlockEdit(path, "instructions", "", "md"), BlockEdit(path, "MCP server", "", "toml")]


def uninstall(
    home: Path,
    env: Mapping[str, str],
    names: list[str] | None,
    dry_run: bool,
    echo: Echo,
    *,
    apply_hint: str | None = APPLY_HINT,
) -> int:
    """Disarm the agents; returns how many files changed (or would, in a dry run).

    apply_hint ends a dry run's count line; the menu, which asks y/N itself, passes None."""
    windows = platform.is_windows()
    launch = Launch(
        hook=str(_launcher_path(home, windows)), mcp=("agentprint", "mcp"), windows=windows
    )
    chosen = list(names) if names else list(ALL_INTEGRATIONS)
    state = State.load(home)
    echo(f"agentprint uninstall{' (dry run: nothing is written)' if dry_run else ''}")
    grouped: dict[Path, list[Edit]] = {}
    for name in chosen:
        # Every agent's hook edits, on Windows too: reverting takes out only agentprint's own
        # entries, including /bin/sh ones an older agentprint wrote there before this gate.
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
        if windows:  # both launchers: an older agentprint wrote the /bin/sh one on Windows too
            grouped.update(_group(_core_edits(home, ["agentprint"], windows=True)))
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
        echo(f"\n{changed} file(s) would change." + (f" {apply_hint}" if apply_hint else ""))
        return changed
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
    return changed


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
        restored = _through_edits(restored, edits, revert=True) or ""
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
        assert entry is not None  # "restored" only happens when a backup was found in entry
        _real(path).write_bytes(Path(entry["backup"]).read_bytes())  # exact original bytes
    else:
        _atomic_write(path, restored)
    state.files.pop(key, None)
    return how
