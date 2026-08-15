"""Stable 63-bit vertex ids from natural keys (blake2b). Not Python hash()."""

from __future__ import annotations

import hashlib
from enum import StrEnum

# 63 bits rather than 64, so the value is always positive when read back as a
# signed integer by Bolt and by anything downstream.
_ID_MASK = (1 << 63) - 1


class Kind(StrEnum):
    """Vertex kind, used as a hash namespace.

    Namespacing means a package and a service that happen to share a name cannot
    collide onto one vertex.
    """

    PACKAGE = "pkg"
    RELEASE = "rel"
    MAINTAINER = "mnt"
    SERVICE = "svc"
    LOCKFILE = "lock"
    ADVISORY = "adv"


def vertex_id(kind: Kind, key: str) -> int:
    """Map a natural key to its HydraDB vertex id."""
    digest = hashlib.blake2b(f"{kind}\x00{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") & _ID_MASK


def package_key(ecosystem: str, name: str) -> str:
    return f"{ecosystem}:{name}"


def release_key(ecosystem: str, name: str, version: str) -> str:
    return f"{ecosystem}:{name}@{version}"


def maintainer_key(ecosystem: str, username: str) -> str:
    return f"{ecosystem}:{username}"


def lockfile_key(service: str, path: str) -> str:
    return f"{service}:{path}"


def release_coord(name: str, version: str) -> str:
    """The readable "name@version" written onto a release as its `key` property.

    The path procedures select endpoints by matching a string property, so this
    is what a traversal names when it needs to target specific releases.
    """
    return f"{name}@{version}"


def package_id(name: str, ecosystem: str = "npm") -> int:
    return vertex_id(Kind.PACKAGE, package_key(ecosystem, name))


def release_id(name: str, version: str, ecosystem: str = "npm") -> int:
    return vertex_id(Kind.RELEASE, release_key(ecosystem, name, version))


def maintainer_id(username: str, ecosystem: str = "npm") -> int:
    return vertex_id(Kind.MAINTAINER, maintainer_key(ecosystem, username))


def service_id(name: str) -> int:
    return vertex_id(Kind.SERVICE, name)


def lockfile_id(service: str, path: str) -> int:
    return vertex_id(Kind.LOCKFILE, lockfile_key(service, path))


def advisory_id(osv_id: str) -> int:
    return vertex_id(Kind.ADVISORY, osv_id)


def edge_id(relationship_type: str, source: int, target: int) -> int:
    """Stable identity for a relationship.

    HydraDB keys `MERGE` on a relationship's own id, so ingest needs one that is
    reproducible from the endpoints. Type is included because two vertices can be
    joined by more than one kind of relationship.
    """
    payload = f"{relationship_type}\x00{source}\x00{target}".encode()
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big") & _ID_MASK
