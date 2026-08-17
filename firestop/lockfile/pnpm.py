"""Parse pnpm-lock.yaml across key-format eras (v6–v9)."""

from __future__ import annotations

from typing import Any

import yaml

from firestop.lockfile import coordinate
from firestop.lockfile.model import Lockfile, LockfileKind, Pin, dedupe

FILENAME = "pnpm-lock.yaml"

_IMPORTER_BLOCKS = ("dependencies", "devDependencies", "optionalDependencies")


def parse(content: bytes, *, path: str = FILENAME, declared: set[str] | None = None) -> Lockfile:
    try:
        document = yaml.safe_load(content.decode("utf-8", errors="replace"))
    except yaml.YAMLError:
        return Lockfile(path=path, kind=LockfileKind.PNPM)
    if not isinstance(document, dict):
        return Lockfile(path=path, kind=LockfileKind.PNPM)

    names = declared if declared is not None else _importer_names(document.get("importers"))
    keys: list[str] = []
    for block in ("packages", "snapshots"):
        entries = document.get(block)
        if isinstance(entries, dict):
            keys.extend(str(key) for key in entries)

    pins = []
    for key in keys:
        name, version = coordinate.split(key)
        if name and version:
            pins.append(Pin(name=name, version=version, direct=name in names))

    return Lockfile(
        path=path,
        kind=LockfileKind.PNPM,
        format_version=str(document.get("lockfileVersion") or ""),
        pins=dedupe(pins),
    )


def _importer_names(importers: Any) -> set[str]:
    if not isinstance(importers, dict):
        return set()

    names: set[str] = set()
    for importer in importers.values():
        if not isinstance(importer, dict):
            continue
        for block_name in _IMPORTER_BLOCKS:
            block = importer.get(block_name)
            if isinstance(block, dict):
                names.update(str(name) for name in block)
    return names
