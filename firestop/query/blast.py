"""Exposure (services → bad releases) and ecosystem reach (reverse)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from firestop.hydra.client import Consistency, HydraClient
from firestop.hydra.values import GraphNode, GraphPath, PathStep
from firestop.query import paths
from firestop.query.compromise import Compromise
from firestop.query.temporal import Window, path_window
from firestop.schema.model import BUILD_RELS, EXPOSURE_RELS, Label, Rel

_SERVICES = "MATCH (s:Service) RETURN s.name AS name, s.criticality AS criticality"

DEFAULT_MAX_LEN = 8
DEFAULT_PATH_COUNT = 4000


@dataclass(frozen=True, slots=True)
class Link:
    """One hop of an exposure path, named the way a person would fix it."""

    relationship: str
    holder: str
    depends_on: str
    package: str
    version: str
    direct: bool = False

    @property
    def actionable(self) -> bool:
        """Whether changing this hop is a thing anybody can actually do.

        A service's link to its own lockfile is not: severing it means deleting
        the service.
        """
        return self.relationship in (str(Rel.PINS), str(Rel.DEPENDS_ON), str(Rel.DEV_DEPENDS_ON))

    @property
    def mine(self) -> bool:
        """A pin lives in a repository the org owns. A dependency edge does not."""
        return self.relationship == str(Rel.PINS)


@dataclass(frozen=True, slots=True)
class ExposurePath:
    """One concrete way a service ends up running a compromised release."""

    service: str
    target: str
    entry: str
    hops: int
    chain: tuple[str, ...]
    links: tuple[Link, ...]
    window: Window
    build_time: bool
    direct: bool

    @property
    def transitive(self) -> bool:
        return not self.direct


@dataclass(frozen=True, slots=True)
class ServiceExposure:
    service: str
    criticality: str
    paths: tuple[ExposurePath, ...]

    @property
    def shortest(self) -> int:
        return min(path.hops for path in self.paths)

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(sorted({path.target for path in self.paths}))

    @property
    def entries(self) -> tuple[str, ...]:
        """The pinned releases the exposure comes in through."""
        return tuple(sorted({path.entry for path in self.paths if path.entry}))

    @property
    def direct(self) -> bool:
        """True when the service declared the compromised package itself."""
        return any(path.direct for path in self.paths)

    @property
    def runtime(self) -> bool:
        """True when at least one path ships to production rather than only CI."""
        return any(not path.build_time for path in self.paths)


@dataclass(slots=True)
class BlastRadius:
    compromise: Compromise
    as_of: int | None
    services: list[ServiceExposure] = field(default_factory=list)
    paths_returned: int = 0
    paths_live: int = 0
    elapsed_ms: float = 0.0
    truncated: bool = False
    depth: int = 0
    asked_for: int = 0

    @property
    def shortened(self) -> bool:
        """The node would not explore as deep as asked, so paths may be missing."""
        return 0 < self.depth < self.asked_for

    @property
    def exposed(self) -> int:
        return len(self.services)

    @property
    def clean(self) -> bool:
        return not self.services


async def exposure(
    client: HydraClient,
    compromise: Compromise,
    *,
    as_of: int | None = None,
    include_build: bool = True,
    max_len: int = DEFAULT_MAX_LEN,
    path_count: int = DEFAULT_PATH_COUNT,
    consistency: Consistency | None = None,
) -> BlastRadius:
    """Which services reach any of the compromised releases, and how."""
    started = time.perf_counter()
    radius = BlastRadius(compromise=compromise, as_of=as_of)

    services = await client.run(_SERVICES, consistency=consistency)
    criticality = {
        str(row["name"]): str(row.get("criticality") or "unknown")
        for row in services.rows
        if row.get("name")
    }
    if not criticality or not compromise.keys:
        radius.elapsed_ms = (time.perf_counter() - started) * 1000
        return radius

    query = paths.PathQuery(
        source=paths.Endpoint(str(Label.SERVICE), "name", tuple(sorted(criticality))),
        target=paths.Endpoint(str(Label.RELEASE), "key", compromise.keys),
        rel_types=_relationship_types(include_build),
        direction="outgoing",
        max_len=max_len,
        path_count=path_count,
    )
    found = await paths.run(client, query, consistency=consistency)

    radius.paths_returned = len(found)
    radius.truncated = len(found) >= path_count
    radius.depth = found.depth
    radius.asked_for = found.asked_for

    by_service: dict[str, list[ExposurePath]] = {}
    for path in found:
        exposure_path = _describe(path)
        if exposure_path is None:
            continue
        if as_of is not None and not exposure_path.window.covers(as_of):
            continue
        if as_of is None and exposure_path.window.empty:
            # The hops never held simultaneously, so this chain never existed.
            continue
        by_service.setdefault(exposure_path.service, []).append(exposure_path)

    radius.paths_live = sum(len(found) for found in by_service.values())
    radius.services = sorted(
        (
            ServiceExposure(
                service=name,
                criticality=criticality.get(name, "unknown"),
                paths=tuple(sorted(found, key=lambda path: (path.hops, path.target))),
            )
            for name, found in by_service.items()
        ),
        key=lambda found: (found.shortest, found.service),
    )
    radius.elapsed_ms = (time.perf_counter() - started) * 1000
    return radius


@dataclass(slots=True)
class Reach:
    """How far a compromise spreads through the ecosystem, ignoring services."""

    compromise: Compromise
    packages: tuple[str, ...] = ()
    releases: int = 0
    max_depth: int = 0
    paths_returned: int = 0
    elapsed_ms: float = 0.0
    truncated: bool = False


async def reach(
    client: HydraClient,
    compromise: Compromise,
    *,
    as_of: int | None = None,
    max_len: int = 6,
    path_count: int = DEFAULT_PATH_COUNT,
    consistency: Consistency | None = None,
) -> Reach:
    """Every release that transitively depends on the compromised ones.

    Traversed backwards along the dependency edges with no target list, which is
    the form that returns every reachable prefix rather than only paths that hit a
    named endpoint.
    """
    started = time.perf_counter()

    query = paths.PathQuery(
        source=paths.Endpoint(str(Label.RELEASE), "key", compromise.keys),
        rel_types=(str(Rel.DEPENDS_ON),),
        direction="incoming",
        max_len=max_len,
        path_count=path_count,
    )
    found = await paths.run(client, query, consistency=consistency)

    packages: set[str] = set()
    releases: set[int] = set()
    depth = 0

    for path in found:
        if as_of is not None and not path_window(path).covers(as_of):
            continue
        depth = max(depth, path.length)
        for node in path.nodes:
            key = node.get("key")
            package = node.get("package")
            if isinstance(package, str) and package:
                packages.add(package)
            if key:
                releases.add(node.id)

    # The compromised releases are in their own reach; the interesting number is
    # what else is.
    packages -= set(compromise.packages)

    return Reach(
        compromise=compromise,
        packages=tuple(sorted(packages)),
        releases=max(len(releases) - len(compromise.keys), 0),
        max_depth=depth,
        paths_returned=len(found),
        truncated=len(found) >= path_count,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _relationship_types(include_build: bool) -> tuple[str, ...]:
    return tuple(str(rel) for rel in (BUILD_RELS if include_build else EXPOSURE_RELS))


def _describe(path: GraphPath) -> ExposurePath | None:
    """Turn a raw path into something a responder can read."""
    if not path.nodes or not path.steps:
        return None

    service = path.nodes[0].get("name")
    target = path.nodes[-1].get("key")
    if not isinstance(service, str) or not isinstance(target, str):
        return None

    entry = ""
    direct = False
    links: list[Link] = []

    for index, step in enumerate(path.steps):
        holder = path.nodes[index] if index < len(path.nodes) else None
        depends_on = path.nodes[index + 1] if index + 1 < len(path.nodes) else None
        if holder is None or depends_on is None:
            continue

        if step.relationship_type == str(Rel.PINS) and not entry:
            entry = str(depends_on.get("key") or "")
            direct = bool(step.get("direct")) and index == len(path.steps) - 1

        links.append(
            Link(
                relationship=step.relationship_type,
                holder=_crumb(holder),
                depends_on=_crumb(depends_on),
                package=str(depends_on.get("package") or ""),
                version=_version_of(depends_on, step),
                direct=bool(step.get("direct")),
            )
        )

    return ExposurePath(
        links=tuple(links),
        service=service,
        target=target,
        entry=entry,
        hops=path.length,
        chain=tuple(_crumb(node) for node in path.nodes),
        window=path_window(path),
        # DEV edge = build-time / CI exposure.
        build_time=any(step.relationship_type == str(Rel.DEV_DEPENDS_ON) for step in path.steps),
        direct=direct,
    )


def _version_of(node: GraphNode, step: PathStep) -> str:
    for candidate in (node.get("version"), step.get("resolved_to")):
        if isinstance(candidate, str) and candidate:
            return candidate
    # Scoped names put @ at the front; version is after the last @.
    key = node.get("key")
    if isinstance(key, str) and "@" in key[1:]:
        return key[1:].rpartition("@")[2]
    return ""


def _crumb(node: GraphNode) -> str:
    for name in ("key", "name", "path"):
        value = node.get(name)
        if isinstance(value, str) and value:
            return value
    return str(node.id)
