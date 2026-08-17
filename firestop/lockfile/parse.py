"""Picking the right parser for a lockfile."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from firestop.lockfile import npm, pnpm, yarn
from firestop.lockfile.model import Lockfile, LockfileKind

_PARSERS = {
    npm.FILENAME: npm.parse,
    yarn.FILENAME: yarn.parse,
    pnpm.FILENAME: pnpm.parse,
}

# Checked in this order when a directory holds more than one, which happens after
# a half-finished migration between package managers.
LOCKFILE_NAMES = (npm.FILENAME, pnpm.FILENAME, yarn.FILENAME)


class UnknownLockfile(ValueError):
    pass


def parse_bytes(
    filename: str, content: bytes, *, path: str = "", declared: set[str] | None = None
) -> Lockfile:
    parser = _PARSERS.get(Path(filename).name)
    if parser is None:
        raise UnknownLockfile(f"no parser for {filename}")
    return parser(content, path=path or filename, declared=declared)


def parse_file(path: Path, *, relative_to: Path | None = None, declared: set[str] | None = None):
    """Parse a lockfile on disk, recording a path that reads like a repository path."""
    shown = str(path.relative_to(relative_to)) if relative_to else str(path)
    if declared is None:
        declared = declared_names(path.parent / "package.json")
    return parse_bytes(
        path.name, path.read_bytes(), path=shown.replace("\\", "/"), declared=declared
    )


def find_lockfile(directory: Path) -> Path | None:
    for name in LOCKFILE_NAMES:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def declared_names(manifest_path: Path) -> set[str] | None:
    """Dependency names from a package.json, or None if there is not one.

    None rather than an empty set, because "no manifest" and "a manifest that
    declares nothing" lead to different guesses about what is direct.
    """
    if not manifest_path.exists():
        return None
    try:
        document = orjson.loads(manifest_path.read_bytes())
    except (OSError, orjson.JSONDecodeError):
        return None
    return _dependency_names(document)


def _dependency_names(document: Any) -> set[str]:
    if not isinstance(document, dict):
        return set()
    names: set[str] = set()
    for block_name in ("dependencies", "devDependencies", "optionalDependencies"):
        block = document.get(block_name)
        if isinstance(block, dict):
            names.update(str(name) for name in block)
    return names


def kind_of(filename: str) -> LockfileKind | None:
    name = Path(filename).name
    if name == npm.FILENAME:
        return LockfileKind.NPM
    if name == yarn.FILENAME:
        return LockfileKind.YARN
    if name == pnpm.FILENAME:
        return LockfileKind.PNPM
    return None
