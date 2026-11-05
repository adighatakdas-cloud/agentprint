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


class PackError(Exception):
    """The package file list could not be produced."""


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
    """

    def __init__(self, label: str, want: Want) -> None:
        self.label, self.want = label, want
        self.files: dict[str, None] = {}  # dicts keep insertion order: an ordered set
        self.contents: dict[str, bytes] = {}
        self.skipped: dict[str, None] = {}
        self.entries = 0
        self.used = 0

    def count(self) -> None:
        self.entries += 1
        if self.entries > MAX_MEMBERS:
            raise PackError(f"archive {self.label} has more than {MAX_MEMBERS} entries")

    def add(self, name: str, size: int, read: Callable[[], bytes] | None) -> None:
        self.files.setdefault(name, None)
        if not self.want(name):
            return
        old = self.contents.pop(name, None)  # a later copy replaces an earlier one
        self.used -= len(old) if old is not None else 0
        self.skipped.pop(name, None)
        if read is None or size > MAX_CONTENT_BYTES or self.used + size > MAX_TOTAL_CONTENT_BYTES:
            self.skipped[name] = None
            return
        data = read()[:size]
        self.used += len(data)
        self.contents[name] = data

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
            cmd, cwd=cwd, capture_output=True, text=True, check=False, timeout=TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise PackError(f"{Path(cmd[0]).name} timed out after {TIMEOUT_SECONDS}s") from exc


def read_npm_project(root: Path, want: Want) -> PackageListing:
    npm = shutil.which("npm")
    if npm is None:
        raise PackError("npm not found. Install Node.js, or pass a built .tgz instead.")
    result = _run([npm, "pack", "--dry-run", "--json", "--ignore-scripts"], cwd=root)
    if result.returncode != 0:
        raise PackError(f"npm pack failed: {result.stderr.strip()[-500:]}")
    try:
        info = json.loads(result.stdout)[0]
        files = [entry["path"] for entry in info["files"]]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise PackError("could not read npm pack output") from exc
    contents: dict[str, bytes] = {}
    for rel in files:
        file = root / rel
        if want(rel) and file.is_file() and file.stat().st_size <= MAX_CONTENT_BYTES:
            contents[rel] = file.read_bytes()
    return PackageListing(label=info.get("filename", root.name), files=files, contents=contents)


def read_python_project(root: Path, want: Want) -> list[PackageListing]:
    uv = shutil.which("uv")
    if uv is None:
        raise PackError(
            "uv not found. Install it from https://docs.astral.sh/uv/, "
            "or pass a built sdist/wheel instead."
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


def collect(target: Path, want: Want) -> list[PackageListing]:
    """Listings for a built archive, or for every package a project directory would publish."""
    if target.is_file():
        return [read_archive(target, want)]
    if not target.is_dir():
        raise PackError(f"{target} does not exist")
    listings: list[PackageListing] = []
    if (target / "package.json").is_file():
        listings.append(read_npm_project(target, want))
    if (target / "pyproject.toml").is_file() or (target / "setup.py").is_file():
        listings.extend(read_python_project(target, want))
    if not listings:
        raise PackError(f"no package.json or pyproject.toml in {target}")
    return listings
