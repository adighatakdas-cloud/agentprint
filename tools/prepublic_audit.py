"""Read-only audit to run before a private repository goes public.

    uv run python -m tools.prepublic_audit [--repo PATH] [--no-network] [--format json]
                                           [--forbid TERM ...]

Extra forbidden terms (names that must not appear in a public repo) come from repeatable
`--forbid TERM` and from an untracked `docs/private/audit-terms.txt` under the audited
repository (one term per line, `#` comments). No term is built into this file.

Exit 0 = PASS, 1 = FAIL, 2 = usage error (bad flag, not a git repository).

It never writes to the repository and never prints a full secret: matches are shown as
`redact()` stubs with the path and a short commit SHA.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agentprint.secrets import find_secrets, redact

MAX_BLOB_BYTES = 1_000_000
# Values containing any of these are deliberate fixtures (see tests/fakes.py, demo/build_demo.py).
FAKE_MARKERS = ("FAKE", "fake_")
PRIVATE_PREFIXES = ("docs/private/", "docs/superpowers/", ".superpowers/", "scans/")
REQUIRED_FILES = ("LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md")
TERMS_FILE = "docs/private/audit-terms.txt"


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped_blobs: int = 0
    scanned_blobs: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


class UsageError(Exception):
    pass


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise UsageError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _is_private_path(path: str) -> bool:
    return path.startswith(PRIVATE_PREFIXES) or path == ".env" or path.endswith("/.env")


def _blob_commits(repo: Path) -> dict[str, str]:
    """Map blob sha -> short sha of a commit that contains it."""
    out = _git(
        repo, "log", "--all", "--raw", "-r", "--no-abbrev", "--no-renames", "-m", "--format=%x00%h"
    ).decode(errors="replace")
    result: dict[str, str] = {}
    commit = "?"
    for line in out.splitlines():
        if line.startswith("\x00"):
            commit = line[1:]
        elif line.startswith(":"):
            fields = line.split()
            if len(fields) >= 4:
                result.setdefault(fields[3], commit)
    return result


def _history_blobs(repo: Path) -> list[tuple[str, str]]:
    out = _git(repo, "rev-list", "--all", "--objects").decode(errors="replace")
    objs: list[tuple[str, str]] = []
    for line in out.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            objs.append((sha, path))
    return objs


def _iter_blobs(repo: Path, objs: list[tuple[str, str]], report: Report):
    """Yield (sha, path, text) for text blobs under the size cap."""
    check = _git(
        repo,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        data="".join(f"{s}\n" for s, _ in objs).encode(),
    ).decode()
    paths = dict(objs)
    wanted: list[str] = []
    for line in check.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        if int(parts[2]) > MAX_BLOB_BYTES:
            report.skipped_blobs += 1
            continue
        wanted.append(parts[0])
    if not wanted:
        return
    raw = _git(repo, "cat-file", "--batch", data="".join(f"{s}\n" for s in wanted).encode())
    pos = 0
    while pos < len(raw):
        nl = raw.index(b"\n", pos)
        sha, _type, size = raw[pos:nl].decode().split()
        body = raw[nl + 1 : nl + 1 + int(size)]
        pos = nl + 1 + int(size) + 1
        if b"\x00" in body[:8000]:
            report.skipped_blobs += 1
            continue
        report.scanned_blobs += 1
        yield sha, paths[sha], body.decode(errors="replace")


def check_history_secrets(repo: Path, report: Report) -> None:
    objs = _history_blobs(repo)
    commits = _blob_commits(repo)
    seen: set[tuple[str, str]] = set()
    for sha, path, text in _iter_blobs(repo, objs, report):
        for m in find_secrets(text):
            if any(mark in m.value for mark in FAKE_MARKERS):
                continue
            stub = redact(m.value)
            key = (path, stub)
            if key in seen:
                continue
            seen.add(key)
            report.problems.append(
                f"secret in history: {m.rule_id} {stub} in {path} (commit {commits.get(sha, '?')})"
            )


def check_private_files(repo: Path, report: Report) -> None:
    tracked = _git(repo, "ls-files", "-z").decode(errors="replace").split("\0")
    for path in sorted(p for p in tracked if p and _is_private_path(p)):
        report.problems.append(f"private file is tracked: {path}")
    history = _git(repo, "log", "--all", "--name-only", "--format=", "--no-renames", "-m")
    ever = {p for p in history.decode(errors="replace").splitlines() if _is_private_path(p)}
    for path in sorted(ever - {p for p in tracked if p}):
        report.problems.append(f"private file exists in history: {path}")


def load_terms(repo: Path, forbid: list[str]) -> list[str]:
    terms = [t.strip() for t in forbid if t.strip()]
    path = repo / TERMS_FILE
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    return list(dict.fromkeys(t.lower() for t in terms))


def check_terms(repo: Path, terms: list[str], report: Report) -> None:
    if not terms:
        return
    args = ["git", "-C", str(repo), "grep", "-I", "-i", "-l", "-F"]
    for term in terms:
        args += ["-e", term]
    grep = subprocess.run(args, capture_output=True, check=False)
    if grep.returncode > 1:
        raise UsageError(f"git grep failed: {grep.stderr.decode(errors='replace').strip()}")
    for path in grep.stdout.decode(errors="replace").splitlines():
        report.problems.append(f"forbidden term in tracked file: {path}")
    log = _git(repo, "log", "--all", "--format=%h%x00%ae%x00%ce%x00%B%x01").decode(errors="replace")
    for entry in log.split("\x01"):
        parts = entry.strip("\n").split("\x00", 3)
        if len(parts) != 4:
            continue
        short, ae, ce, body = parts
        if any(t in ae.lower() or t in ce.lower() for t in terms):
            report.problems.append(f"forbidden term in author/committer of commit {short}")
        if any(t in body.lower() for t in terms):
            report.problems.append(f"forbidden term in commit message {short}")
    seen: set[str] = set()
    for _sha, path, text in _iter_blobs(repo, _history_blobs(repo), Report()):
        low = text.lower()
        if path not in seen and any(t in low for t in terms):
            seen.add(path)
            report.problems.append(f"forbidden term in history blob: {path}")


def check_community_files(repo: Path, report: Report) -> None:
    tracked = set(_git(repo, "ls-files", "-z").decode(errors="replace").split("\0"))
    for name in REQUIRED_FILES:
        if name not in tracked:
            report.problems.append(f"missing community file: {name}")


def check_github_metadata(repo: Path, report: Report) -> None:
    try:
        proc = subprocess.run(
            ["gh", "api", "repos/{owner}/{repo}"],
            cwd=repo,
            capture_output=True,
            check=False,
            timeout=30,
        )
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        data = None
    if not isinstance(data, dict):
        report.notes.append("repo description/topics: skipped (gh or network unavailable)")
        return
    if not str(data.get("description") or "").strip():
        report.problems.append("GitHub repo description is not set")
    if not data.get("topics"):
        report.problems.append("GitHub repo topics are not set")


def run_audit(repo: Path, network: bool = True, forbid: list[str] | None = None) -> Report:
    if not repo.is_dir():
        raise UsageError(f"not a directory: {repo}")
    try:
        _git(repo, "rev-parse", "--git-dir")
    except UsageError as e:
        raise UsageError(f"not a git repository: {repo}") from e
    report = Report()
    if _git(repo, "rev-parse", "--all").strip():
        check_history_secrets(repo, report)
        check_private_files(repo, report)
        check_terms(repo, load_terms(repo, forbid or []), report)
    else:
        report.problems.append("repository has no commits")
    check_community_files(repo, report)
    if network:
        check_github_metadata(repo, report)
    else:
        report.notes.append("repo description/topics: skipped (--no-network)")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prepublic_audit", description=(__doc__ or "").split("\n")[0]
    )
    parser.add_argument("--repo", default=".", help="repository to audit (default: .)")
    parser.add_argument("--no-network", action="store_true", help="skip the gh api check")
    parser.add_argument(
        "--forbid", action="append", default=[], metavar="TERM", help="extra forbidden term"
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        report = run_audit(Path(args.repo), network=not args.no_network, forbid=args.forbid)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(
            json.dumps(
                {
                    "result": "PASS" if report.ok else "FAIL",
                    "problems": report.problems,
                    "notes": report.notes,
                    "blobs_scanned": report.scanned_blobs,
                    "blobs_skipped": report.skipped_blobs,
                },
                indent=2,
            )
        )
    else:
        print(
            f"scanned {report.scanned_blobs} blobs "
            f"({report.skipped_blobs} skipped: binary or over {MAX_BLOB_BYTES} bytes)"
        )
        for note in report.notes:
            print(f"note: {note}")
        for problem in report.problems:
            print(f"FAIL: {problem}")
        print("PASS" if report.ok else f"FAIL ({len(report.problems)} problems)")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
