"""`agentprint protect`: check pushes and publishes at git and npm themselves, per project.

The agent hook (`agentprint install`) reads the command an agent is about to run. Reading text
can always be dodged: `eval "$CMD"`, or a push built at run time inside `python -c`. `protect`
adds a second layer at the tools' own boundary, which fires however the command was started:

- git: a pre-push hook in the repository runs `agentprint push-check`, which reads git's ref
  lines on stdin and runs the same push check `guard` runs for `git push`. A pre-push hook that
  was already there is kept byte for byte as `pre-push.agentprint-chained` and runs first, with
  the same arguments and stdin; either one failing stops the push.
- npm: package.json gets (or chains onto) `scripts.prepublishOnly`, which runs
  `npx --yes agentprint-cli@<version> pack --fail-on high` before `npm publish`. `pack` lists
  the package with `npm pack --dry-run --ignore-scripts`, so it never runs itself again.

PyPI has no such boundary: twine and `uv publish` run no publish hook, so only the agent hook
and `agentprint pack` cover Python publishes.

Opt-in and reversible: what protect wrote is recorded in ~/.agentprint/protect.json (with
package.json's original bytes), and `agentprint protect --undo` puts each file back exactly,
or, when a file has changed since, leaves it alone and says what to remove by hand.

Ruling (where the hook goes): `git rev-parse --git-path hooks`, which follows worktrees and
core.hooksPath. A core.hooksPath inside the work tree belongs to a hook manager (husky,
lefthook) or a tracked folder, so nothing is written there: the line to add is printed
instead. One outside the repository is written to only when it is a folder this user owns
(POSIX); otherwise, and always on Windows, the line is printed. Cost if wrong: one manual line.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from . import __version__, platform
from .advice import Moment
from .gitcheck import _git, check_push, repo_root
from .guard import _masked, strict_block, strict_requested
from .install import (
    STATE_DIR,
    _read,
    _read_only,
    _through_edits,
    changes,
    pinned_uvx,
    reported_version,
    resolve_agentprint,
)
from .integrations.base import JsonEdit, Unsupported
from .models import CannotCheck
from .report import FailOn, _short, exit_code, to_agent
from .safewrite import atomic_write
from .sanitize import clean

Echo = Callable[[str], None]

MARK = "agentprint-protect"
HOOK = "pre-push"
CHAINED = "pre-push.agentprint-chained"
STATE_FILE = "protect.json"
NPM_WRAPPER = "agentprint-cli"
PUSH_CHECK_SINCE = (0, 1, 1)  # the first agentprint with `push-check`
_ZERO_SHA = re.compile(r"0{40,}")


def npm_command() -> str:
    """The prepublishOnly command protect adds, pinned to this release."""
    return f"npx --yes {NPM_WRAPPER}@{__version__} pack --fail-on high"


def _ours_in_script(script: str) -> bool:
    return f"{NPM_WRAPPER}@" in script and " pack" in script


# ---- the git hook -----------------------------------------------------------------------------


def hook_agentprint() -> list[str]:
    """How the hook starts agentprint: as `agentprint install` decides, except that a permanent
    install too old to have `push-check` is passed over for uvx running this version."""
    agentprint = resolve_agentprint()
    if agentprint[1:] == [f"agentprint=={__version__}"]:
        return agentprint
    version = reported_version(agentprint)
    return agentprint if version is not None and version >= PUSH_CHECK_SINCE else pinned_uvx()


def _sh_argv(agentprint: list[str]) -> list[str]:
    """Git for Windows runs hooks with its own sh, which reads C:/x/y.exe but not C:\\x\\y.exe."""
    if not platform.is_windows() or not agentprint:
        return agentprint
    return [PureWindowsPath(agentprint[0]).as_posix(), *agentprint[1:]]


def hook_script(agentprint: list[str], strict: bool) -> str:
    """The pre-push hook (POSIX sh, LF line ends: Git for Windows runs it with its bundled sh)."""
    command = " ".join(shlex.quote(part) for part in _sh_argv(agentprint))
    flag = " --strict" if strict else ""
    return f"""#!/bin/sh
# {MARK}: written by `agentprint protect`; `agentprint protect --undo` removes it.
# Before every git push, however it was started (typed, run from a script, or hidden in eval),
# this runs agentprint's push check and stops the push if a key or an agent file would go out.
# A pre-push hook that was here before is kept as {CHAINED} and runs first.
# If agentprint is missing or fails, this says so and lets the push go, unless strict mode is on
# (`agentprint protect --strict`, or AGENTPRINT_STRICT=1): then it blocks instead.
strict={1 if strict else 0}
case "$(printf '%s' "${{AGENTPRINT_STRICT:-}}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) strict=1 ;;
esac
refs=$(cat)
feed() {{ [ -z "$refs" ] || printf '%s\\n' "$refs"; }}
chained="$(dirname "$0")/{CHAINED}"
if [ -x "$chained" ]; then
  feed | "$chained" "$@" || exit $?
fi
out=$(feed | {command} push-check{flag})
status=$?
[ -z "$out" ] || printf '%s\\n' "$out" >&2
case "$status" in
  0) exit 0 ;;
  1) case "$out" in "agentprint: BLOCK"*) exit 1 ;; esac ;;
esac
if [ "$strict" = 1 ]; then
  echo "agentprint: the push check could not run (exit $status); push blocked (strict mode)" >&2
  exit 1
fi
echo "agentprint: the push check could not run (exit $status); push allowed" >&2
exit 0
"""


def manual_line(strict: bool) -> str:
    """The command to add to a hook manager's pre-push hook by hand (portable: no local paths)."""
    return f"uvx agentprint=={__version__} push-check" + (" --strict" if strict else "")


@dataclass(frozen=True)
class Repo:
    root: Path  # top of the work tree
    hooks: Path  # where git looks for hooks
    common: Path  # the repository's common git folder (shared by worktrees)
    hooks_path_set: bool  # core.hooksPath is configured


def _absolute(root: Path, out: str | None) -> Path | None:
    text = (out or "").strip()
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else root / path


def find_repo(path: Path) -> Repo | None:
    """The git work tree `path` is in, or None (not a repository, a bare one, or no git)."""
    root = repo_root(path)
    if root is None:
        return None
    hooks = _absolute(root, _git(root, "rev-parse", "--git-path", "hooks"))
    common = _absolute(root, _git(root, "rev-parse", "--git-common-dir"))
    if hooks is None or common is None:
        return None
    configured = bool((_git(root, "config", "--get", "core.hooksPath") or "").strip())
    return Repo(root, hooks, common, configured)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def _owned_dir(folder: Path) -> bool:
    """A folder this user owns (POSIX only: on Windows nothing outside the repo is written)."""
    if platform.is_windows():
        return False
    try:
        return folder.is_dir() and folder.stat().st_uid == os.geteuid()
    except OSError:
        return False


def _hook_folder(repo: Repo) -> tuple[Path | None, str]:
    """(the folder to write the hook in, a note) or (None, why the line is printed instead)."""
    if not repo.hooks_path_set or _inside(repo.hooks, repo.common):
        return repo.hooks, ""
    if _inside(repo.hooks, repo.root):
        return None, "managed"
    if _owned_dir(repo.hooks):
        return repo.hooks, "shared"
    return None, "not-owned"


def _manager_advice(repo: Repo, why: str, strict: bool) -> list[str]:
    """What to add by hand when the hook folder belongs to a hook manager or someone else."""
    line = manual_line(strict)
    shown = _short(str(repo.hooks))
    lines = [f"git: core.hooksPath is set to {shown}, so agentprint did not add a hook there."]
    lefthook = [n for n in ("lefthook.yml", ".lefthook.yml") if (repo.root / n).is_file()]
    if why == "managed" and ".husky" in repo.hooks.parts:
        husky = repo.root / ".husky" / HOOK
        lines.append(f"  To check every push, add this line to {_short(str(husky))}:")
        lines.append(f"    {line}")
    elif why == "managed" and lefthook:
        lines.append(
            f"  To check every push, add this to {lefthook[0]} and run `lefthook install`:"
        )
        lines += ["    pre-push:", "      jobs:", f"        - run: {line}"]
    else:
        owner = "a folder inside the repository" if why == "managed" else "a folder you don't own"
        lines.append(f"  It is {owner}. To check every push, add this line to its {HOOK} hook:")
        lines.append(f"    {line}")
    lines.append(
        "  It reads git's pre-push input on stdin (husky passes it on); without that input, "
        "deleting a branch is checked too."
    )
    return lines


# ---- state: what protect wrote ----------------------------------------------------------------


def _state_path(home: Path) -> Path:
    return home / STATE_DIR / STATE_FILE


def _load(home: Path) -> dict[str, dict]:
    try:
        data = json.loads(_state_path(home).read_text(encoding="utf-8"))
        entries = data.get("entries", {}) if isinstance(data, dict) else {}
        return {k: v for k, v in entries.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _save(home: Path, entries: dict[str, dict]) -> None:
    path = _state_path(home)
    if not entries:
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            path.parent.rmdir()  # only when nothing else of agentprint's is left in it
        return
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
    atomic_write(path, json.dumps({"version": 1, "entries": entries}, indent=2) + "\n", 0o600)


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _key(path: Path) -> str:
    return str(path.parent.resolve() / path.name)


# ---- protect ----------------------------------------------------------------------------------


def protect(
    path: Path,
    home: Path,
    echo: Echo,
    *,
    strict: bool = False,
    agentprint: list[str] | None = None,
) -> int:
    """Add the git hook and the npm script. 0 = done (or already there), 1 = something needs
    doing by hand (printed), 2 = nothing here to protect."""
    if not path.is_dir():
        echo(f"error: {clean(str(path))} does not exist or is not a folder")
        return 2
    repo = find_repo(path)
    package = path / "package.json"
    if repo is None and not package.is_file():
        echo(
            f"agentprint protect: {clean(_short(str(path)))} is not in a git repository and has "
            "no package.json, so there is nothing to protect here."
        )
        return 2
    entries = _load(home)
    echo(f"agentprint protect: {clean(_short(str(path)))}")
    needs_you = 0
    try:
        if repo is not None:
            needs_you += _layer("git", echo, _protect_git, repo, entries, echo, strict, agentprint)
        else:
            echo("git: not a git repository, so no pre-push hook was added.")
        if package.is_file():
            needs_you += _layer("npm", echo, _protect_npm, package, entries, echo)
    finally:
        _save(home, entries)
    _python_note(path, echo)
    echo(f"Undo with: agentprint protect --undo {shlex.quote(str(path))}")
    return 1 if needs_you else 0


def _layer(name: str, echo: Echo, step: Callable[..., int], *args: object) -> int:
    """Run one layer; a file that can't be read or written stops only that layer, said once."""
    try:
        return step(*args)
    except OSError as exc:
        echo(f"{name}: stopped, a file could not be read or written ({exc}). Nothing was lost.")
        return 1


def _protect_git(
    repo: Repo, entries: dict[str, dict], echo: Echo, strict: bool, agentprint: list[str] | None
) -> int:
    folder, why = _hook_folder(repo)
    if folder is None:
        for line in _manager_advice(repo, why, strict):
            echo(line)
        return 1
    hook, chained = folder / HOOK, folder / CHAINED
    shown = clean(_short(str(hook)))
    argv = agentprint or hook_agentprint()
    content = hook_script(argv, strict)
    data = content.encode("utf-8")
    key = _key(hook)
    current = _bytes(hook)
    entry = entries.get(key)
    if current is not None and MARK.encode() in current:
        if current == data:
            echo(f"git: already protected: {shown}")
            return 0
        if entry is None or _sha(current) != entry.get("sha256"):
            why = "has no record of it" if entry is None else "was changed since protect wrote it"
            echo(f"git: {shown} is agentprint's hook but {why}, so it was left as is.")
            echo("  " + _by_hand_git(hook, chained) + " Then run agentprint protect again.")
            return 1
        atomic_write(hook, content)
        entry["sha256"] = _sha(data)
        echo(f"git: updated the pre-push hook: {shown}" + (" (strict mode)" if strict else ""))
        return 0
    if os.path.lexists(chained):
        echo(
            f"git: {clean(_short(str(chained)))} already exists, so agentprint left {shown} as "
            "is. Move that file away, then run agentprint protect again."
        )
        return 1
    if hook.is_dir():
        echo(f"git: {shown} is a folder, so no hook was added.")
        return 1
    created = _make_dirs(folder)
    keep = os.path.lexists(hook)
    if keep:
        os.rename(hook, chained)  # the same file: bytes, mode and symlinks untouched
    try:
        atomic_write(hook, content, 0o755)
    except BaseException:
        if keep:  # never leave your own hook renamed with nothing in its place
            os.replace(chained, hook)
        raise
    entries[key] = {
        "kind": "git-hook",
        "repo": str(repo.common.resolve()),
        "sha256": _sha(data),
        "chained": str(chained) if keep else None,
        "chained_sha256": _sha(_bytes(chained)) if keep else None,
        "created_dirs": created,
    }
    echo(f"git: added a pre-push hook: {shown}" + (" (strict mode)" if strict else ""))
    echo("  Every git push from this repository now runs agentprint push-check first, however")
    echo("  the push is started (typed, from a script, or hidden from the agent hook).")
    if keep:
        echo(f"  Your own pre-push hook is kept as {clean(_short(str(chained)))} and runs first.")
    if why == "shared":
        echo(
            f"  Note: core.hooksPath points to {clean(_short(str(folder)))}, outside this "
            "repository, so the check runs for every repository that uses that folder."
        )
    echo("  The hook starts agentprint with: " + clean(" ".join(_sh_argv(argv))))
    return 0


def _make_dirs(folder: Path) -> list[str]:
    created: list[str] = []
    missing = []
    while not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for d in reversed(missing):
        d.mkdir()
        created.append(str(d))
    return created


def _add_script(data: dict) -> None:
    scripts = data.setdefault("scripts", {})
    if not isinstance(scripts, dict):
        raise Unsupported('its "scripts" value is not a set of named scripts')
    current = scripts.get("prepublishOnly")
    if current is None or (isinstance(current, str) and not current.strip()):
        scripts["prepublishOnly"] = npm_command()
    elif not isinstance(current, str):
        raise Unsupported('its "scripts.prepublishOnly" value is not a command')
    elif not _ours_in_script(current):
        scripts["prepublishOnly"] = f"{current.rstrip()} && {npm_command()}"


def _drop_script(data: dict) -> None:  # JsonEdit needs one; undo restores the saved bytes
    pass


def _npm_edit(package: Path) -> JsonEdit:
    return JsonEdit(package, "prepublishOnly", _add_script, _drop_script)


def _by_hand_npm() -> str:
    return (
        f'In package.json, take " && {npm_command()}" out of scripts.prepublishOnly (or the '
        "whole prepublishOnly script, if that is all it runs)."
    )


def _protect_npm(package: Path, entries: dict[str, dict], echo: Echo) -> int:
    edit = _npm_edit(package)
    try:
        old = _read(package)
        new = _through_edits(old, [edit])
        if new != old and _read_only(package):
            raise Unsupported("it is read-only")
    except Unsupported as exc:
        echo(f"npm: package.json left as is because {exc}. To check every npm publish, add:")
        echo(f'  "scripts": {{ "prepublishOnly": "{npm_command()}" }}')
        return 1
    if old is None or new is None:
        return 0
    if new == old:
        echo("npm: already protected: package.json scripts.prepublishOnly runs agentprint pack")
        _yarn_berry_note(package.parent, echo)
        return 0
    atomic_write(package, new)
    entries[_key(package)] = {
        "kind": "npm",
        "before": old,
        "sha256": _sha(new.encode("utf-8")),
    }
    echo("npm: package.json scripts.prepublishOnly now runs agentprint pack before npm publish:")
    for line in changes(old, new, [edit]):
        echo(line)
    _yarn_berry_note(package.parent, echo)
    return 0


def _yarn_berry_note(folder: Path, echo: Echo) -> None:
    """Yarn 2+ runs `prepublish`, not `prepublishOnly`, on `yarn npm publish`."""
    try:
        manager = json.loads((folder / "package.json").read_text(encoding="utf-8"))
        manager = manager.get("packageManager", "") if isinstance(manager, dict) else ""
    except (OSError, ValueError):
        manager = ""
    berry = re.match(r"yarn@([2-9]|\d{2,})\.", str(manager)) or (folder / ".yarnrc.yml").is_file()
    if berry:
        echo(
            "  Note: Yarn 2+ (`yarn npm publish`) does not run prepublishOnly. The agent hook "
            "(agentprint install) still checks that publish."
        )


def _python_note(path: Path, echo: Echo) -> None:
    if (path / "pyproject.toml").is_file() or (path / "setup.py").is_file():
        echo(
            "PyPI: twine upload and uv publish have no publish hook, so nothing can be added "
            "for them. The agent hook (agentprint install) checks those publishes; run "
            "`agentprint pack dist/<file>` on a built package yourself."
        )


# ---- undo -------------------------------------------------------------------------------------


def unprotect(path: Path, home: Path, echo: Echo) -> int:
    """Take out what protect added. 0 = done (or nothing to do), 1 = something was left for
    you to remove by hand (printed), 2 = no such folder."""
    if not path.is_dir():
        echo(f"error: {clean(str(path))} does not exist or is not a folder")
        return 2
    entries = _load(home)
    echo(f"agentprint protect --undo: {clean(_short(str(path)))}")
    left = 0
    try:
        repo = find_repo(path)
        if repo is not None:
            left += _layer("git", echo, _undo_git, repo, entries, echo)
        if (path / "package.json").is_file() or _key(path / "package.json") in entries:
            left += _layer("npm", echo, _undo_npm, path / "package.json", entries, echo)
    finally:
        _save(home, entries)
    return 1 if left else 0


def _by_hand_git(hook: Path, chained: Path | None) -> str:
    text = f"delete {clean(_short(str(hook)))}"
    if chained is not None and os.path.lexists(chained):
        text += f", then rename {clean(_short(str(chained)))} back to {HOOK}"
    return f"To remove it by hand: {text}."


def _undo_git(repo: Repo, entries: dict[str, dict], echo: Echo) -> int:
    folder, _ = _hook_folder(repo)
    here = _key(folder / HOOK) if folder is not None else None
    repo_id = str(repo.common.resolve())
    mine = [
        key
        for key, entry in entries.items()
        if entry.get("kind") == "git-hook" and (key == here or entry.get("repo") == repo_id)
    ]
    if not mine:
        hook = (folder or repo.hooks) / HOOK
        current = _bytes(hook)
        if current is not None and MARK.encode() in current:
            echo(f"git: {clean(_short(str(hook)))} was added by agentprint protect, but there is ")
            echo("  no record of it (was the repository moved?), so it was left as is.")
            echo("  " + _by_hand_git(hook, (folder or repo.hooks) / CHAINED))
            return 1
        echo("git: no agentprint pre-push hook here.")
        return 0
    return sum(_undo_hook(key, entries, echo) for key in mine)


def _undo_hook(key: str, entries: dict[str, dict], echo: Echo) -> int:
    hook, entry = Path(key), entries[key]
    chained = Path(entry["chained"]) if entry.get("chained") else None
    shown = clean(_short(str(hook)))
    current = _bytes(hook)
    if current is None or _sha(current) != entry.get("sha256"):
        what = "is gone" if current is None else "was changed since protect wrote it"
        echo(f"git: {shown} {what}, so agentprint left it as is.")
        if chained is not None and os.path.lexists(chained):
            echo(
                f"  Your own hook is still at {clean(_short(str(chained)))}: rename it back to "
                f"{HOOK} when you are ready."
            )
        elif current is not None:
            echo("  " + _by_hand_git(hook, None))
        if current is None:
            entries.pop(key)
        return 1
    if chained is not None:
        if _sha(_bytes(chained)) != entry.get("chained_sha256"):
            echo(
                f"git: {clean(_short(str(chained)))} (your own hook) is missing or was changed "
                "since, so agentprint left both files as they are. " + _by_hand_git(hook, chained)
            )
            return 1
        os.replace(chained, hook)  # your hook back under its own name, exactly as it was
        echo(f"git: removed agentprint's pre-push hook; your own hook is back at {shown}.")
    else:
        hook.unlink()
        created: list[str] = [str(d) for d in entry.get("created_dirs") or []]
        for folder in sorted(created, key=len, reverse=True):
            try:
                Path(folder).rmdir()
            except OSError:
                break
        echo(f"git: removed agentprint's pre-push hook: {shown}")
    entries.pop(key)
    return 0


def _undo_npm(package: Path, entries: dict[str, dict], echo: Echo) -> int:
    key = _key(package)
    entry = entries.get(key)
    current = _bytes(package)
    if entry is None:
        try:
            text = (current or b"").decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        if f"{NPM_WRAPPER}@" in text:
            echo("npm: package.json runs agentprint-cli, but there is no record of protect adding")
            echo("  it, so it was left as is. " + _by_hand_npm())
            return 1
        echo("npm: nothing of agentprint's in package.json.")
        return 0
    if current is None:
        echo("npm: package.json is gone; nothing to undo.")
        entries.pop(key)
        return 0
    if _sha(current) != entry.get("sha256"):
        echo("npm: package.json was changed since protect edited it, so it was left as is.")
        echo("  " + _by_hand_npm())
        return 1
    atomic_write(package, entry["before"])  # the original bytes, formatting and line ends
    entries.pop(key)
    echo("npm: package.json is back exactly as it was.")
    return 0


# ---- what `status` and `scan` say -------------------------------------------------------------


def _mentions_push_check(path: Path) -> bool:
    data = _bytes(path)
    return data is not None and (
        MARK.encode() in data or (b"agentprint" in data and b"push-check" in data)
    )


def protection(path: Path) -> list[str]:
    """What checks pushes and publishes for the project at `path` at git and npm themselves:
    some of "git pre-push hook" and "npm prepublishOnly". Empty when nothing does (or when it
    cannot tell). Never raises."""
    found: list[str] = []
    try:
        repo = find_repo(path) if path.is_dir() else None
        if repo is not None:
            places = [repo.hooks / HOOK]
            if repo.hooks.name == "_":  # husky 9: the hooks you edit sit one folder up
                places.append(repo.hooks.parent / HOOK)
            places += [repo.root / n for n in ("lefthook.yml", ".lefthook.yml")]
            if any(_mentions_push_check(p) for p in places):
                found.append("git pre-push hook")
        data = json.loads((path / "package.json").read_text(encoding="utf-8"))
        script = data.get("scripts", {}).get("prepublishOnly") if isinstance(data, dict) else None
        if isinstance(script, str) and _ours_in_script(script):
            found.append("npm prepublishOnly")
    except (OSError, ValueError, AttributeError):
        pass
    return found


def protection_line(path: Path) -> str | None:
    """One line for `status` and `scan` when the project is protected; None when it is not."""
    found = protection(path)
    return protection_text(found) if found else None


def protection_text(found: list[str]) -> str:
    return "protected (agentprint protect): " + ", ".join(found)


# ---- push-check (run by the hook) -------------------------------------------------------------


def uploads(refs: str | None) -> bool:
    """False only when git's ref lines show nothing but deletes (an all-zero local sha). No
    lines (a terminal, or a hook manager that did not pass them on) counts as uploading."""
    if refs is None:
        return True
    lines = [line.split() for line in refs.splitlines() if line.strip()]
    if not lines:
        return True
    return any(len(parts) < 2 or not _ZERO_SHA.fullmatch(parts[1]) for parts in lines)


def push_check(
    cwd: Path,
    refs: str | None,
    env: Mapping[str, str],
    fail_on: FailOn = FailOn.high,
    strict: bool = False,
) -> tuple[int, str]:
    """(exit code, report) for `agentprint push-check`: 1 blocks the push, 0 lets it go."""
    strict = strict or strict_requested(env)
    if not uploads(refs):
        return 0, "agentprint: OK — this push only deletes remote branches or tags."
    try:
        findings = check_push(cwd)
    except CannotCheck as exc:
        why = f"agentprint could not check this git push: {clean(_masked(str(exc)))}."
        if strict:
            return 1, strict_block(why).reason.rstrip("\n")
        return 0, f"{why} The push was allowed."
    except Exception as exc:  # noqa: BLE001 - an agentprint bug must not block (unless strict)
        why = f"agentprint hit an internal error ({type(exc).__name__}) and could not check it."
        if strict:
            return 1, strict_block(why).reason.rstrip("\n")
        return 0, f"{why} The push was allowed. Please report this."
    return exit_code(findings, fail_on), to_agent(findings, fail_on, Moment.push, rerun="git push")
