"""List exactly which files a package would publish, using the package manager itself."""

from __future__ import annotations

import io
import json
import lzma
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from . import platform

MAX_CONTENT_BYTES = 10 * 1024 * 1024  # largest single file whose content is read
MAX_TOTAL_CONTENT_BYTES = 64 * 1024 * 1024  # all content read from one archive, together
MAX_MEMBERS = 100_000  # entries (files, folders, links) in one archive
MAX_UNPACKED_BYTES = 2 * 1024**3  # a .tar.gz must be fully unpacked just to list its names
TIMEOUT_SECONDS = 300
# Everything a damaged, truncated or unusual archive can raise while being read.
ARCHIVE_ERRORS = (
    OSError,
    EOFError,
    ValueError,
    RuntimeError,  # includes NotImplementedError and encrypted zip members
    tarfile.TarError,
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
)
Want = Callable[[str], bool]


def _nothing(name: str) -> bool:
    return False


@dataclass(frozen=True)
class Wants:
    """Two `want` predicates used as one, in order of priority.

    `first` files are read exactly as if `first` were the only predicate: they keep the whole
    content budget. `extra` files are read only from the room `first` leaves, and give it back
    (moving to `skipped`) when a later `first` file needs it. So extra reads can never stop an
    agent file from being read for keys, and one archive never holds more than
    MAX_TOTAL_CONTENT_BYTES of content.
    """

    first: Want
    extra: Want

    def __call__(self, name: str) -> bool:
        return self.first(name) or self.extra(name)


def _split(want: Want) -> tuple[Want, Want]:
    if isinstance(want, Wants):
        return want.first, want.extra
    return want, _nothing


class PackError(Exception):
    """The package file list could not be produced. `unavailable`: because a tool agentprint
    runs (npm, uv) is not installed, not because of the package."""

    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        super().__init__(message)
        self.unavailable = unavailable


@dataclass(frozen=True)
class PackageListing:
    label: str  # shown before "!" in finding locations
    files: list[str]
    contents: dict[str, bytes] = field(default_factory=dict)  # only files `want` asked for
    skipped: list[str] = field(default_factory=list)  # wanted, but content not read (see below)


class _Collector:
    """Builds a listing from archive entries, within fixed limits.

    - More than MAX_MEMBERS entries: PackError, because a cut-off file list could hide agent files.
    - Content over MAX_CONTENT_BYTES (one file) or MAX_TOTAL_CONTENT_BYTES (whole archive) is not
      read; the name is still listed and also put in `skipped`. Links are listed, never followed.
    - A name that appears twice is listed once, with the content of its last copy: that is the
      copy tar, unzip, npm and pip leave on disk. Copies are never mixed together.
    - With `Wants`, `extra` files share the budget but always yield it to `first` files.
    """

    def __init__(self, label: str, want: Want) -> None:
        self.label = label
        self.first, self.extra = _split(want)
        self.files: dict[str, None] = {}  # dicts keep insertion order: an ordered set
        self.contents: dict[str, bytes] = {}
        self.skipped: dict[str, None] = {}
        self.extras: dict[str, int] = {}  # extra files read so far, in order, with their size
        self.entries = 0
        self.used = 0  # content held for `first` files
        self.used_extra = 0  # content held for `extra` files

    def count(self) -> None:
        self.entries += 1
        if self.entries > MAX_MEMBERS:
            raise PackError(f"archive {self.label} has more than {MAX_MEMBERS} entries")

    def add(self, name: str, size: int, read: Callable[[], bytes] | None) -> None:
        self.files.setdefault(name, None)
        first = self.first(name)
        if not first and not self.extra(name):
            return
        self._drop(name)  # a later copy replaces an earlier one
        if read is None or size > MAX_CONTENT_BYTES:
            self.skipped[name] = None
            return
        if first:
            if self.used + size > MAX_TOTAL_CONTENT_BYTES:
                self.skipped[name] = None
                return
            while self.extras and self.used + self.used_extra + size > MAX_TOTAL_CONTENT_BYTES:
                self._give_back()
        elif self.used + self.used_extra + size > MAX_TOTAL_CONTENT_BYTES:
            self.skipped[name] = None
            return
        data = read()[:size]
        self.contents[name] = data
        if first:
            self.used += len(data)
        else:
            self.extras[name] = len(data)
            self.used_extra += len(data)

    def _drop(self, name: str) -> None:
        old = self.contents.pop(name, None)
        if name in self.extras:
            self.used_extra -= self.extras.pop(name)
        elif old is not None:
            self.used -= len(old)
        self.skipped.pop(name, None)

    def _give_back(self) -> None:
        """Drop the latest extra file's content to make room for a `first` file."""
        name, size = self.extras.popitem()
        del self.contents[name]
        self.used_extra -= size
        self.skipped[name] = None

    def listing(self) -> PackageListing:
        return PackageListing(self.label, list(self.files), self.contents, list(self.skipped))


def _read_zip(fileobj: IO[bytes], out: _Collector) -> None:
    # A zip lists its names in one directory at the end, so only wanted files are unpacked.
    with zipfile.ZipFile(fileobj) as zf:
        for info in zf.infolist():
            out.count()
            name = info.filename.replace("\\", "/")  # zips made on Windows can use backslashes
            if not name.endswith("/"):
                out.add(name, info.file_size, lambda info=info: zf.read(info))


def _read_tar(fileobj: IO[bytes], out: _Collector) -> None:
    unpacked = 0
    with tarfile.open(fileobj=fileobj, mode="r:*") as tf:
        for member in tf:  # one forward pass; entries are counted before they pile up
            out.count()
            unpacked += member.size
            if unpacked > MAX_UNPACKED_BYTES:
                raise PackError(
                    f"archive {out.label} unpacks to more than {MAX_UNPACKED_BYTES} bytes"
                )
            if member.isfile():
                handle = tf.extractfile(member)
                out.add(member.name, member.size, handle.read if handle else None)
            elif member.issym() or member.islnk():
                out.add(member.name, 0, None)


def read_archive_bytes(data: bytes, label: str, want: Want) -> PackageListing:
    """Read a downloaded .tgz/.tar.gz/.whl/.zip held in memory. `label` names its type."""
    return read_archive_fileobj(io.BytesIO(data), label, want)


def read_archive(path: Path, want: Want) -> PackageListing:
    """Read a .tgz/.tar.gz/.whl/.zip in memory. Nothing is extracted to disk."""
    try:
        with path.open("rb") as fh:
            return read_archive_fileobj(fh, path.name, want)
    except OSError as exc:
        raise PackError(f"cannot read archive {path}: {exc}") from exc


def read_archive_fileobj(fileobj: IO[bytes], label: str, want: Want) -> PackageListing:
    """Read any seekable binary file object. `label` (a file name) decides zip or tar."""
    out = _Collector(label, want)
    try:
        if label.endswith((".whl", ".zip")):
            _read_zip(fileobj, out)
        else:
            _read_tar(fileobj, out)
    except ARCHIVE_ERRORS as exc:
        raise PackError(f"cannot read archive {label}: {exc}") from exc
    return out.listing()


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=TIMEOUT_SECONDS,
            **platform.subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise PackError(f"{Path(cmd[0]).name} timed out after {TIMEOUT_SECONDS}s") from exc


def read_npm_project(root: Path, want: Want) -> PackageListing:
    npm = shutil.which("npm")
    if npm is None:
        raise PackError(
            "npm not found. Install Node.js, or pass a built .tgz instead.", unavailable=True
        )
    result = _run([npm, "pack", "--dry-run", "--json", "--ignore-scripts"], cwd=root)
    if result.returncode != 0:
        raise PackError(f"npm pack failed: {result.stderr.strip()[-500:]}")
    try:
        info = json.loads(result.stdout)[0]
        files = [entry["path"] for entry in info["files"]]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise PackError("could not read npm pack output") from exc
    first, extra = _split(want)
    contents: dict[str, bytes] = {}
    skipped: list[str] = []
    room = MAX_TOTAL_CONTENT_BYTES  # for extra files; `first` files are read as they always were
    for rel in files:
        file = root / rel
        if first(rel):
            if file.is_file() and file.stat().st_size <= MAX_CONTENT_BYTES:
                contents[rel] = file.read_bytes()
        elif extra(rel):
            data = _read_extra(file, room)
            if data is None:
                skipped.append(rel)
            else:
                contents[rel] = data
                room -= len(data)
    label = info.get("filename", root.name)
    return PackageListing(label=label, files=files, contents=contents, skipped=skipped)


def _read_extra(file: Path, room: int) -> bytes | None:
    """An extra file's content, or None when it is missing, unreadable, or over a limit."""
    try:
        if not file.is_file() or file.stat().st_size > min(MAX_CONTENT_BYTES, room):
            return None
        data = file.read_bytes()
    except OSError:
        return None
    return data if len(data) <= min(MAX_CONTENT_BYTES, room) else None


def read_python_project(root: Path, want: Want) -> list[PackageListing]:
    uv = shutil.which("uv")
    if uv is None:
        raise PackError(
            "uv not found. Install it from https://docs.astral.sh/uv/, "
            "or pass a built sdist/wheel instead.",
            unavailable=True,
        )
    with tempfile.TemporaryDirectory() as tmp:
        # Unlike npm --ignore-scripts, a Python build runs the project's build backend code.
        result = _run([uv, "build", "--out-dir", tmp, str(root)])
        if result.returncode != 0:
            raise PackError(f"uv build failed: {result.stderr.strip()[-500:]}")
        # uv also writes a .gitignore into the output dir; keep only the built packages.
        archives = sorted(p for p in Path(tmp).iterdir() if p.name.endswith((".tar.gz", ".whl")))
        if not archives:
            raise PackError("uv build produced no files")
        return [read_archive(archive, want) for archive in archives]


def collect_each(
    target: Path, want: Want, *, npm_only: bool = False
) -> tuple[list[PackageListing], list[PackError]]:
    """Listings for a built archive, or for every package a project directory would publish,
    and what could not be listed. Each ecosystem is read on its own, so one that fails never
    hides another's files. `npm_only`: leave a Python project in the same folder out."""
    if target.is_file():
        return [read_archive(target, want)], []
    if not target.is_dir():
        raise PackError(f"{target} does not exist")
    listings: list[PackageListing] = []
    errors: list[PackError] = []
    readers: list = []
    if (target / "package.json").is_file():
        readers.append(lambda: [read_npm_project(target, want)])
    python = (target / "pyproject.toml").is_file() or (target / "setup.py").is_file()
    if python and not npm_only:
        readers.append(lambda: read_python_project(target, want))
    if not readers:
        raise PackError(f"no package.json or pyproject.toml in {target}")
    for read in readers:
        try:
            listings.extend(read())
        except PackError as exc:
            errors.append(exc)
    return listings, errors


def collect(target: Path, want: Want) -> list[PackageListing]:
    """Listings for a built archive, or for every package a project directory would publish.
    Raises the first PackError when any part could not be listed."""
    listings, errors = collect_each(target, want)
    if errors:
        raise errors[0]
    return listings
