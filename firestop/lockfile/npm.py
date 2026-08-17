"""Parse package-lock.json v1–v3 (prefer flat `packages` map when present)."""

from __future__ import annotations

from typing import Any

import orjson

from firestop.lockfile import coordinate
from firestop.lockfile.model import Lockfile, LockfileKind, Pin, dedupe

FILENAME = "package-lock.json"


def parse(content: bytes, *, path: str = FILENAME, declared: set[str] | None = None) -> Lockfile:
    """Parse a lockfile. `declared` is the service's own manifest, when known."""
    lockfile = Lockfile(path=path, kind=LockfileKind.NPM)

    try:
        document = orjson.loads(content)
    except orjson.JSONDecodeError:
        return lockfile
    if not isinstance(document, dict):
        return lockfile

    lockfile.format_version = str(document.get("lockfileVersion") or "")
    packages = document.get("packages")

    if isinstance(packages, dict) and packages:
        root = packages.get("") if isinstance(packages.get(""), dict) else {}
        names = declared if declared is not None else _declared(root)
        lockfile.pins = dedupe(_from_packages(packages, names))
    else:
        lockfile.pins = dedupe(_from_tree(document.get("dependencies"), declared or set()))

    return lockfile


def _from_packages(packages: dict[str, Any], declared: set[str]) -> list[Pin]:
    pins: list[Pin] = []

    for install_path, entry in packages.items():
        if not install_path or not isinstance(entry, dict):
            # The empty key is the service's own manifest, not a dependency.
            continue
        # A workspace link points at a sibling directory rather than a published
        # release, so there is nothing in the registry to attach it to.
        if entry.get("link"):
            continue

        name = str(entry.get("name") or coordinate.from_module_path(install_path))
        version = str(entry.get("version") or "")
        if not name or not version:
            continue

        # Only a top-level install can be a direct dependency. The same package
        # nested under another is somebody else's choice.
        hoisted = install_path.count("node_modules/") == 1
        pins.append(Pin(name=name, version=version, direct=hoisted and name in declared))

    return pins


def _from_tree(tree: Any, declared: set[str], *, top: bool = True) -> list[Pin]:
    if not isinstance(tree, dict):
        return []

    pins: list[Pin] = []
    for name, entry in tree.items():
        if not isinstance(entry, dict):
            continue
        version = str(entry.get("version") or "")
        if version:
            pins.append(Pin(name=name, version=version, direct=top and name in declared))
        pins.extend(_from_tree(entry.get("dependencies"), declared, top=False))
    return pins


def _declared(manifest: Any) -> set[str]:
    if not isinstance(manifest, dict):
        return set()
    names: set[str] = set()
    for block_name in ("dependencies", "devDependencies", "optionalDependencies"):
        block = manifest.get(block_name)
        if isinstance(block, dict):
            names.update(str(name) for name in block)
    return names
