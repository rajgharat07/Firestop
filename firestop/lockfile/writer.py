"""Building graph rows from a service's lockfile."""

from __future__ import annotations

from dataclasses import dataclass, field

from firestop.ids import edge_id, lockfile_id, service_id
from firestop.lockfile.model import Lockfile
from firestop.lockfile.org import Service
from firestop.schema.index import ReleaseIndex
from firestop.schema.model import Rel


@dataclass(slots=True)
class LockfileRows:
    services: list[dict] = field(default_factory=list)
    lockfiles: list[dict] = field(default_factory=list)
    uses_lockfile: list[dict] = field(default_factory=list)
    pins: list[dict] = field(default_factory=list)
    # Pinned releases the graph has never heard of. Expected -- the crawl covers a
    # slice of npm, not all of it -- and worth reporting rather than hiding.
    unknown: list[str] = field(default_factory=list)

    def extend(self, other: LockfileRows) -> None:
        self.services.extend(other.services)
        self.lockfiles.extend(other.lockfiles)
        self.uses_lockfile.extend(other.uses_lockfile)
        self.pins.extend(other.pins)
        self.unknown.extend(other.unknown)

    def __len__(self) -> int:
        return len(self.services) + len(self.lockfiles) + len(self.uses_lockfile) + len(self.pins)


def service_rows(service: Service, lockfile: Lockfile, releases: ReleaseIndex) -> LockfileRows:
    rows = LockfileRows()
    service_vertex = service_id(service.name)
    lockfile_vertex = lockfile_id(service.name, lockfile.path)

    rows.services.append(
        {
            "id": service_vertex,
            "name": service.name,
            "repo": service.repo,
            "criticality": service.criticality,
        }
    )
    rows.lockfiles.append(
        {
            "id": lockfile_vertex,
            "path": lockfile.path,
            "service": service.name,
            "committed_at": service.committed_at,
        }
    )
    rows.uses_lockfile.append(
        {
            "id": edge_id(str(Rel.USES_LOCKFILE), service_vertex, lockfile_vertex),
            "source": service_vertex,
            "target": lockfile_vertex,
        }
    )

    for pin in lockfile.pins:
        release = (releases.get(pin.name) or {}).get(pin.version)
        if release is None:
            rows.unknown.append(f"{pin.name}@{pin.version}")
            continue

        rows.pins.append(
            {
                "id": edge_id(str(Rel.PINS), lockfile_vertex, release),
                "source": lockfile_vertex,
                "target": release,
                "resolved_version": pin.version,
                "direct": pin.direct,
            }
        )

    return rows
