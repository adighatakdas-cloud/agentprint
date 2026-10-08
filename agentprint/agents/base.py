"""What every agent module provides."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from ..files import walk_files
from ..models import Artifact, Kind, PackageRule


class Agent(Protocol):
    name: str
    package_rules: tuple[PackageRule, ...]

    def discover_home(self, home: Path) -> Iterator[Artifact]: ...

    def discover_project(self, root: Path) -> Iterator[Artifact]: ...


def artifacts(agent: str, kind: Kind, *paths: Path) -> Iterator[Artifact]:
    """Every existing file at or under each path, tagged with its agent and kind."""
    for path in paths:
        for file in walk_files(path):
            yield Artifact(agent=agent, kind=kind, path=file)
