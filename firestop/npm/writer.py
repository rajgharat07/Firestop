"""Map packuments to Hydra upsert rows (testable without the network)."""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass, field

from firestop.ids import (
    edge_id,
    maintainer_id,
    package_id,
    release_coord,
    release_id,
)
from firestop.npm.packument import Packument
from firestop.npm.resolve import Resolver
from firestop.schema.model import DependencyKind, Rel, relationship_for

ECOSYSTEM = "npm"


@dataclass(slots=True)
class VertexRows:
    """The part of a package that can be written before its dependencies exist."""

    packages: list[dict] = field(default_factory=list)
    releases: list[dict] = field(default_factory=list)
    maintainers: list[dict] = field(default_factory=list)
    version_of: list[dict] = field(default_factory=list)
    can_publish: list[dict] = field(default_factory=list)
    published: list[dict] = field(default_factory=list)

    def extend(self, other: VertexRows) -> None:
        self.packages.extend(other.packages)
        self.releases.extend(other.releases)
        self.maintainers.extend(other.maintainers)
        self.version_of.extend(other.version_of)
        self.can_publish.extend(other.can_publish)
        self.published.extend(other.published)

    def __len__(self) -> int:
        return (
            len(self.packages)
            + len(self.releases)
            + len(self.maintainers)
            + len(self.version_of)
            + len(self.can_publish)
            + len(self.published)
        )


def vertex_rows(packument: Packument) -> VertexRows:
    """Rows for one package: itself, its releases, and who publishes it."""
    rows = VertexRows()
    name = packument.name
    pkg = package_id(name)

    rows.packages.append(
        {
            "id": pkg,
            "name": name,
            "ecosystem": ECOSYSTEM,
            "first_published": packument.first_published,
            # Filled in once the dependency edges are known; see dependent_counts.
            "dependent_count": 0,
        }
    )

    for release in packument.releases:
        release_vertex = release_id(name, release.version)
        rows.releases.append(
            {
                "id": release_vertex,
                "key": release_coord(name, release.version),
                "package": name,
                "version": release.version,
                "published_at": release.published_at,
                "integrity": release.integrity,
                "deprecated": release.deprecated,
            }
        )
        rows.version_of.append(_edge(Rel.VERSION_OF, release_vertex, pkg))

        if release.publisher:
            publisher = maintainer_id(release.publisher)
            rows.maintainers.append(_maintainer(release.publisher, ""))
            rows.published.append(
                _edge(Rel.PUBLISHED, publisher, release_vertex, at=release.published_at)
            )

    for username, email in packument.maintainers:
        rows.maintainers.append(_maintainer(username, email))
        rows.can_publish.append(_edge(Rel.CAN_PUBLISH, maintainer_id(username), pkg))

    return rows


def dependency_rows(
    packument: Packument,
    version_times: dict[str, dict[str, int]],
    resolver: Resolver,
    known_releases: Container[int],
    *,
    with_dev: bool = True,
) -> dict[Rel, list[dict]]:
    """Dependency rows for one package's releases, grouped by relationship type.

    Needs the dependency's own version timeline to resolve a range, which is why
    this cannot run during the same pass that discovers the package.

    `with_dev` is off for packages nobody in the graph builds. A dev dependency
    is installed for the package being built and never for something that merely
    depends on it, so a dev edge deep in the tree describes an install that
    cannot happen -- and there are several times more of them than real ones.

    `known_releases` is not optional caution. HydraDB refuses a relationship
    upsert whose endpoint does not exist, and resolution deliberately runs over a
    package's *whole* publish history while only its most recent manifests are
    written as vertices. So a range can legitimately resolve to a release that
    was never stored, and that edge has to be dropped rather than invented: the
    alternative is claiming a resolution to some older kept version, which would
    be a lie about what the install actually did.
    """
    best: dict[int, dict] = {}
    relationships: dict[int, Rel] = {}
    name = packument.name

    for release in packument.releases:
        source = release_id(name, release.version)
        if source not in known_releases:
            continue

        for dependency in release.dependencies:
            timeline = version_times.get(dependency.name)
            if not timeline:
                # The dependency was never crawled at all.
                continue

            relationship = relationship_for(dependency.kind)
            if relationship is Rel.DEV_DEPENDS_ON and not with_dev:
                continue

            windows = resolver.windows_for(
                dependency.name, dependency.range, timeline, release.published_at
            )
            for window in windows:
                target = release_id(dependency.name, window.version)
                if target not in known_releases:
                    resolver.unwritten_targets += 1
                    continue

                row = _edge(
                    relationship,
                    source,
                    target,
                    range=dependency.range,
                    resolved_to=window.version,
                    kind=str(dependency.kind),
                    valid_from=window.valid_from,
                    valid_to=window.valid_to,
                )

                # One release can declare the same dependency under two kinds --
                # `dependencies` and `peerDependencies` both naming react is
                # ordinary -- and both can resolve to the same release. That is
                # one relationship id with two candidate property sets, and
                # HydraDB rejects a batch carrying an id twice with differing
                # properties. Strongest declaration wins, because exposure
                # follows whatever actually gets installed.
                existing = best.get(row["id"])
                if existing is None or _kind_rank(row) < _kind_rank(existing):
                    best[row["id"]] = row
                    relationships[row["id"]] = relationship

    grouped: dict[Rel, list[dict]] = {Rel.DEPENDS_ON: [], Rel.DEV_DEPENDS_ON: []}
    for edge_key, row in best.items():
        grouped[relationships[edge_key]].append(row)
    return grouped


# Lower is stronger. A runtime dependency is always installed; a dev dependency
# of a dependency never is.
_KIND_PRECEDENCE = {
    str(DependencyKind.RUNTIME): 0,
    str(DependencyKind.PEER): 1,
    str(DependencyKind.OPTIONAL): 2,
    str(DependencyKind.DEV): 3,
}


def _kind_rank(row: dict) -> int:
    return _KIND_PRECEDENCE.get(row["kind"], len(_KIND_PRECEDENCE))


def collect_dependents(
    edges: list[dict], release_owner: dict[int, str], into: dict[str, set[str]]
) -> None:
    """Accumulate which packages depend on each package.

    Dependents are tracked as a set of names rather than a running total because
    edges are written in batches: one dependent's edges can straddle two flushes,
    and adding per-batch counts would count that dependent twice.

    Counted over packages rather than releases, because sixty releases of one
    dependent is still one team with one decision to make.
    """
    for edge in edges:
        dependent = release_owner.get(edge["source"])
        dependency = release_owner.get(edge["target"])
        if dependent and dependency and dependent != dependency:
            into.setdefault(dependency, set()).add(dependent)


def dependent_count_rows(dependents: dict[str, set[str]]) -> list[dict]:
    """Rows for the narrow update statement, which touches only this one property.

    Reusing the full package upsert here would rewrite `first_published` and
    `name` from values this pass does not have.
    """
    return [
        {"id": package_id(name), "dependent_count": len(names)}
        for name, names in dependents.items()
    ]


def _maintainer(username: str, email: str) -> dict:
    return {
        "id": maintainer_id(username),
        "username": username,
        "ecosystem": ECOSYSTEM,
        "email": email,
    }


def _edge(relationship: Rel, source: int, target: int, **properties) -> dict:
    return {
        "id": edge_id(str(relationship), source, target),
        "source": source,
        "target": target,
        **properties,
    }
