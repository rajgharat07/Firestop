"""Slim packument: versions, times, maintainers, per-version _npmUser."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from firestop.schema.model import DependencyKind
from firestop.times import parse_timestamp

# Registry field name to the kind recorded on the edge. `bundleDependencies` is
# skipped: it is a list of names already declared elsewhere, not a range map.
_DEPENDENCY_FIELDS: tuple[tuple[str, DependencyKind], ...] = (
    ("dependencies", DependencyKind.RUNTIME),
    ("devDependencies", DependencyKind.DEV),
    ("peerDependencies", DependencyKind.PEER),
    ("optionalDependencies", DependencyKind.OPTIONAL),
)


@dataclass(frozen=True, slots=True)
class Dependency:
    name: str
    range: str
    kind: DependencyKind


@dataclass(slots=True)
class Release:
    version: str
    published_at: int
    integrity: str = ""
    deprecated: bool = False
    publisher: str = ""
    dependencies: list[Dependency] = field(default_factory=list)

    @property
    def runtime_dependencies(self) -> list[Dependency]:
        return [d for d in self.dependencies if d.kind is DependencyKind.RUNTIME]


@dataclass(slots=True)
class Packument:
    name: str
    releases: list[Release] = field(default_factory=list)
    maintainers: list[tuple[str, str]] = field(default_factory=list)
    # Publish time for every version, including ones whose manifest was dropped.
    # Resolution windows need the full timeline even when the manifest is not
    # kept, because a version we never expand can still be what a range resolved
    # to at some point.
    version_times: dict[str, int] = field(default_factory=dict)

    @property
    def first_published(self) -> int:
        return min(self.version_times.values(), default=0)

    def dependency_names(self) -> set[str]:
        return {d.name for release in self.releases for d in release.dependencies}


def parse_packument(document: dict[str, Any], *, max_versions: int = 40) -> Packument:
    """Reduce a registry document, keeping the most recent `max_versions` manifests."""
    name = str(document.get("name") or "")
    times = _version_times(document.get("time"))
    versions = document.get("versions") or {}

    # Ordered by publish time so "most recent" is by release date rather than by
    # semver, which matters for packages that backport onto old majors.
    known = sorted(
        (version for version in versions if version in times),
        key=lambda version: (times[version], version),
    )
    kept = known[-max_versions:] if max_versions > 0 else known

    releases = []
    for version in kept:
        manifest = versions.get(version)
        if isinstance(manifest, dict):
            releases.append(_release(version, times[version], manifest))

    return Packument(
        name=name,
        releases=releases,
        maintainers=_maintainers(document.get("maintainers")),
        version_times=times,
    )


def _release(version: str, published_at: int, manifest: dict[str, Any]) -> Release:
    dist = manifest.get("dist") if isinstance(manifest.get("dist"), dict) else {}
    npm_user = manifest.get("_npmUser") if isinstance(manifest.get("_npmUser"), dict) else {}

    return Release(
        version=version,
        published_at=published_at,
        integrity=str(dist.get("integrity") or dist.get("shasum") or ""),
        # The registry writes a deprecation message here, not a boolean, and an
        # empty string is used to un-deprecate.
        deprecated=bool(manifest.get("deprecated")),
        publisher=str(npm_user.get("name") or ""),
        dependencies=_dependencies(manifest),
    )


def _dependencies(manifest: dict[str, Any]) -> list[Dependency]:
    found: list[Dependency] = []
    for field_name, kind in _DEPENDENCY_FIELDS:
        ranges = manifest.get(field_name)
        if not isinstance(ranges, dict):
            continue
        for name, spec in ranges.items():
            if isinstance(name, str) and isinstance(spec, str) and name:
                found.append(Dependency(name=name, range=spec, kind=kind))
    return found


def _version_times(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    times = {}
    for version, stamp in raw.items():
        # `created` and `modified` sit in the same map as real versions.
        if version in ("created", "modified") or not isinstance(stamp, str):
            continue
        epoch = parse_timestamp(stamp)
        if epoch is not None:
            times[version] = epoch
    return times


def _maintainers(raw: Any) -> list[tuple[str, str]]:
    if not isinstance(raw, list):
        return []
    people = []
    for entry in raw:
        if isinstance(entry, dict) and entry.get("name"):
            people.append((str(entry["name"]), str(entry.get("email") or "")))
        elif isinstance(entry, str) and entry:
            # Older documents carry "name <email>" strings.
            people.append((entry.split("<", 1)[0].strip(), ""))
    return people
