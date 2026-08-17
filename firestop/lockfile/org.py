"""Load the demo org manifest (services + lockfile paths + criticality)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import orjson

from firestop.lockfile.parse import find_lockfile
from firestop.times import parse_timestamp

MANIFEST_NAME = "org.json"


@dataclass(frozen=True, slots=True)
class Service:
    name: str
    repo: str = ""
    criticality: str = "unknown"
    directory: Path = Path()
    lockfile: Path | None = None
    committed_at: int = 0


@dataclass(frozen=True, slots=True)
class Org:
    name: str
    root: Path
    services: tuple[Service, ...] = ()


class InvalidOrg(ValueError):
    pass


def load(root: Path) -> Org:
    """Read an org manifest and locate each service's lockfile."""
    manifest_path = root / MANIFEST_NAME if root.is_dir() else root
    try:
        document = orjson.loads(manifest_path.read_bytes())
    except (OSError, orjson.JSONDecodeError) as exc:
        raise InvalidOrg(f"cannot read {manifest_path}") from exc

    base = manifest_path.parent
    entries = document.get("services")
    if not isinstance(entries, list) or not entries:
        raise InvalidOrg(f"{manifest_path} declares no services")

    services = tuple(
        service for service in (_service(entry, base) for entry in entries) if service is not None
    )
    if not services:
        raise InvalidOrg(f"{manifest_path} declares no service with a lockfile")

    return Org(name=str(document.get("org") or base.name), root=base, services=services)


def _service(entry: object, base: Path) -> Service | None:
    if not isinstance(entry, dict):
        return None

    name = str(entry.get("name") or "")
    if not name:
        return None

    directory = base / str(entry.get("path") or name)
    lockfile = find_lockfile(directory)
    if lockfile is None:
        return None

    return Service(
        name=name,
        repo=str(entry.get("repo") or ""),
        criticality=str(entry.get("criticality") or "unknown"),
        directory=directory,
        lockfile=lockfile,
        committed_at=_committed_at(entry.get("committed_at"), lockfile),
    )


def _committed_at(declared: object, lockfile: Path) -> int:
    """When this lockfile became what the service was running.

    Stated in the manifest where it is known, because a file's modification time
    on a fresh clone is the time of the clone and says nothing about the service.
    """
    if isinstance(declared, str) and declared:
        parsed = parse_timestamp(declared)
        if parsed is not None:
            return parsed
    if isinstance(declared, int):
        return declared
    return int(lockfile.stat().st_mtime)
