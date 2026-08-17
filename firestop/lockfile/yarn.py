"""Parse yarn.lock — Berry (YAML) and classic (hand-scanned)."""

from __future__ import annotations

import re

import yaml

from firestop.lockfile import coordinate
from firestop.lockfile.model import Lockfile, LockfileKind, Pin, dedupe

FILENAME = "yarn.lock"

_VERSION_LINE = re.compile(r'^\s+"?version"?\s*[:\s]\s*"?([^"\s]+)"?\s*$')
_ENTRY_HEADER = re.compile(r"^(?!\s)(.+):\s*$")


def parse(content: bytes, *, path: str = FILENAME, declared: set[str] | None = None) -> Lockfile:
    text = content.decode("utf-8", errors="replace")
    names = declared or set()

    if "__metadata:" in text:
        return _berry(text, path, names)
    return _classic(text, path, names)


def _classic(text: str, path: str, declared: set[str]) -> Lockfile:
    pins: list[Pin] = []
    specs: list[str] = []

    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        header = _ENTRY_HEADER.match(line)
        if header:
            specs = [spec.strip().strip('"') for spec in _split_specs(header.group(1))]
            continue

        found = _VERSION_LINE.match(line)
        if found and specs:
            version = found.group(1)
            for spec in specs:
                name, _ = coordinate.split(spec)
                if name:
                    pins.append(Pin(name=name, version=version, direct=name in declared))
            specs = []

    return Lockfile(path=path, kind=LockfileKind.YARN, format_version="1", pins=dedupe(pins))


def _berry(text: str, path: str, declared: set[str]) -> Lockfile:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return Lockfile(path=path, kind=LockfileKind.YARN)
    if not isinstance(document, dict):
        return Lockfile(path=path, kind=LockfileKind.YARN)

    metadata = document.get("__metadata")
    format_version = ""
    if isinstance(metadata, dict):
        format_version = str(metadata.get("version") or "")

    pins: list[Pin] = []
    for key, entry in document.items():
        if key == "__metadata" or not isinstance(entry, dict):
            continue

        version = str(entry.get("version") or "")
        if not version:
            continue

        for spec in _split_specs(str(key)):
            name, _ = coordinate.split(spec)
            # Workspace members resolve to themselves and have no release.
            if name and "workspace:" not in spec:
                pins.append(Pin(name=name, version=version, direct=name in declared))

    return Lockfile(
        path=path, kind=LockfileKind.YARN, format_version=format_version, pins=dedupe(pins)
    )


def _split_specs(header: str) -> list[str]:
    """One entry can answer several specs, listed comma-separated."""
    return [spec.strip().strip('"') for spec in header.split(",") if spec.strip()]
