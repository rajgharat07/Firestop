"""Turn an advisory id or package+range into a set of compromised Release.keys."""

from __future__ import annotations

from dataclasses import dataclass, field

import nodesemver as semver

from firestop.hydra.client import Consistency, HydraClient
from firestop.ids import release_coord

_ADVISORY_RELEASES = (
    "MATCH (a:Advisory)-[r:AFFECTS]->(rel:Release) WHERE a.osv_id = $osv_id "
    "RETURN rel.key AS key, rel.package AS package, rel.version AS version, "
    "rel.published_at AS published_at, r.fixed_in AS fixed_in"
)

_PACKAGE_RELEASES = (
    "MATCH (rel:Release) WHERE rel.package = $package "
    "RETURN rel.key AS key, rel.version AS version, rel.published_at AS published_at"
)


@dataclass(frozen=True, slots=True)
class Compromise:
    """The releases under suspicion, and where that suspicion came from."""

    packages: tuple[str, ...]
    keys: tuple[str, ...]
    advisory: str = ""
    summary: str = ""
    severity: str = ""
    fixed_in: tuple[str, ...] = ()
    # When the bad code first became available, which is the earliest moment any
    # exposure could have begun.
    introduced_at: int = 0
    versions: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.keys)

    @property
    def unfixed(self) -> bool:
        return not self.fixed_in


class UnknownCompromise(LookupError):
    pass


async def from_advisory(
    client: HydraClient, osv_id: str, *, consistency: Consistency | None = None
) -> Compromise:
    """Every release an advisory marks as vulnerable, as the graph recorded it."""
    header = await client.run(
        "MATCH (a:Advisory) WHERE a.osv_id = $osv_id "
        "RETURN a.summary AS summary, a.severity AS severity",
        {"osv_id": osv_id},
        consistency=consistency,
    )
    if not header.rows:
        raise UnknownCompromise(f"no advisory {osv_id} in the graph")

    affected = await client.run(_ADVISORY_RELEASES, {"osv_id": osv_id}, consistency=consistency)
    if not affected.rows:
        raise UnknownCompromise(f"advisory {osv_id} affects no release in the graph")

    versions: dict[str, list[str]] = {}
    fixes: set[str] = set()
    earliest = 0

    for row in affected.rows:
        package = str(row.get("package") or "")
        version = str(row.get("version") or "")
        if not package or not version:
            continue
        versions.setdefault(package, []).append(version)

        fixed_in = str(row.get("fixed_in") or "")
        if fixed_in:
            fixes.add(fixed_in)

        published = row.get("published_at")
        if isinstance(published, int) and published > 0:
            earliest = published if earliest == 0 else min(earliest, published)

    return Compromise(
        packages=tuple(sorted(versions)),
        keys=_keys(versions),
        advisory=osv_id,
        summary=str(header.rows[0].get("summary") or ""),
        severity=str(header.rows[0].get("severity") or ""),
        fixed_in=tuple(sorted(fixes)),
        introduced_at=earliest,
        versions={name: tuple(sorted(found)) for name, found in versions.items()},
    )


async def from_range(
    client: HydraClient,
    package: str,
    spec: str = "*",
    *,
    consistency: Consistency | None = None,
) -> Compromise:
    """Releases of one package matching a semver range.

    This is the shape of a live incident: somebody says "anything from 1.4.0 up is
    backdoored" before any advisory exists.
    """
    result = await client.run(_PACKAGE_RELEASES, {"package": package}, consistency=consistency)
    if not result.rows:
        raise UnknownCompromise(f"no releases of {package} in the graph")

    matched: list[str] = []
    earliest = 0

    for row in result.rows:
        version = str(row.get("version") or "")
        if not version or not _satisfies(version, spec):
            continue
        matched.append(version)

        published = row.get("published_at")
        if isinstance(published, int) and published > 0:
            earliest = published if earliest == 0 else min(earliest, published)

    if not matched:
        raise UnknownCompromise(f"no release of {package} satisfies {spec!r}")

    versions = {package: sorted(matched)}
    return Compromise(
        packages=(package,),
        keys=_keys(versions),
        summary=f"{package}@{spec}",
        introduced_at=earliest,
        versions={package: tuple(sorted(matched))},
    )


def _keys(versions: dict[str, list[str]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            release_coord(package, version)
            for package, found in versions.items()
            for version in found
        )
    )


def _satisfies(version: str, spec: str) -> bool:
    if spec in ("", "*"):
        return True
    try:
        return bool(semver.satisfies(version, spec, loose=True))
    except Exception:
        return False
