"""Building paths the way a node returns them, for tests that need traversal results."""

from __future__ import annotations

from typing import Any

from firestop.schema.model import OPEN_INTERVAL_END


def node(vertex: int, label: str, **properties: Any) -> dict:
    return {
        "id": vertex,
        "labels": [label],
        "properties": {name: _tag(value) for name, value in properties.items()},
    }


def hop(relationship: str, src: int, dst: int, **properties: Any) -> dict:
    return {
        "id": src ^ dst,
        "edge_type": relationship,
        "src": src,
        "dst": dst,
        "properties": {name: _tag(value) for name, value in properties.items()},
    }


def path(nodes: list[dict], relationships: list[dict]) -> dict:
    """One `{"type": "path"}` row value."""
    return {"type": "path", "value": {"nodes": nodes, "relationships": relationships}}


def service_path(
    service: str,
    *,
    package: str,
    version: str,
    lockfile: str = "checkout-api/package-lock.json",
    hops: list[tuple[str, str]] | None = None,
    valid_from: int = 0,
    valid_to: int = OPEN_INTERVAL_END,
    direct: bool = False,
    dev: bool = False,
) -> dict:
    """A service reaching one release, optionally through intermediate packages.

    `hops` names the releases between the pinned entry and the target, as
    (name, version) pairs, so a test can say "two dependency hops" without
    assembling vertex ids by hand.
    """
    chain = [*(hops or []), (package, version)]
    entry_name, entry_version = chain[0]

    service_vertex = _vertex(service)
    lockfile_vertex = _vertex(lockfile)
    nodes = [
        node(service_vertex, "Service", name=service),
        node(lockfile_vertex, "Lockfile", path=lockfile, service=service),
    ]
    relationships = [hop("USES_LOCKFILE", service_vertex, lockfile_vertex)]

    entry_vertex = _vertex(f"{entry_name}@{entry_version}")
    nodes.append(
        node(entry_vertex, "Release", key=f"{entry_name}@{entry_version}", package=entry_name)
    )
    relationships.append(
        hop(
            "PINS",
            lockfile_vertex,
            entry_vertex,
            resolved_version=entry_version,
            direct=direct,
        )
    )

    previous = entry_vertex
    for name, release in chain[1:]:
        vertex = _vertex(f"{name}@{release}")
        nodes.append(node(vertex, "Release", key=f"{name}@{release}", package=name))
        relationships.append(
            hop(
                "DEV_DEPENDS_ON" if dev else "DEPENDS_ON",
                previous,
                vertex,
                resolved_to=release,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )
        previous = vertex

    return path(nodes, relationships)


def release_path(
    *chain: tuple[str, str],
    valid_from: int = 0,
    valid_to: int = OPEN_INTERVAL_END,
) -> dict:
    """A dependency chain between releases, as a reverse or forward traversal returns it."""
    nodes = []
    relationships = []
    previous = 0

    for name, version in chain:
        vertex = _vertex(f"{name}@{version}")
        nodes.append(
            node(vertex, "Release", key=f"{name}@{version}", package=name, version=version)
        )
        if previous:
            relationships.append(
                hop(
                    "DEPENDS_ON",
                    previous,
                    vertex,
                    resolved_to=version,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            )
        previous = vertex

    return path(nodes, relationships)


def _vertex(key: str) -> int:
    return abs(hash(key)) % (1 << 62) + 1


def _tag(value: Any) -> dict:
    if isinstance(value, bool):
        return {"Bool": value}
    if isinstance(value, int):
        return {"Integer": value}
    if isinstance(value, float):
        return {"Float": value}
    return {"String": str(value)}
