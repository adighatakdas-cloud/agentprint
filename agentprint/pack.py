"""List exactly which files a package would publish, using the package manager itself."""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MAX_CONTENT_BYTES = 10 * 1024 * 1024
Want = Callable[[str], bool]


class PackError(Exception):
    """The package file list could not be produced."""


@dataclass(frozen=True)
class PackageListing:
    label: str  # shown before "!" in finding locations
    files: list[str]
    contents: dict[str, bytes] = field(default_factory=dict)  # only files `want` asked for


def read_archive(path: Path, want: Want) -> PackageListing:
    """Read a .tgz/.tar.gz/.whl/.zip in memory. Nothing is extracted to disk."""
    contents: dict[str, bytes] = {}
    try:
        if path.name.endswith((".whl", ".zip")):
            with zipfile.ZipFile(path) as zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                files = [i.filename for i in infos]
                for info in infos:
                    if want(info.filename) and info.file_size <= MAX_CONTENT_BYTES:
                        contents[info.filename] = zf.read(info)
        else:
            with tarfile.open(path, "r:*") as tf:
                members = [m for m in tf.getmembers() if m.isfile()]
                files = [m.name for m in members]
                for member in members:
                    if want(member.name) and member.size <= MAX_CONTENT_BYTES:
                        handle = tf.extractfile(member)
                        if handle is not None:
                            contents[member.name] = handle.read()
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise PackError(f"cannot read archive {path}: {exc}") from exc
    return PackageListing(label=path.name, files=files, contents=contents)


def read_npm_project(root: Path, want: Want) -> PackageListing:
    npm = shutil.which("npm")
    if npm is None:
        raise PackError("npm not found. Install Node.js, or pass a built .tgz instead.")
    result = subprocess.run(
        [npm, "pack", "--dry-run", "--json", "--ignore-scripts"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
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
        result = subprocess.run(
            [uv, "build", "--out-dir", tmp, str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
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
