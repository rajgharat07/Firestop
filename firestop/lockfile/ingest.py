"""Loading an org's lockfiles into the graph."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from firestop.hydra.client import HydraClient
from firestop.lockfile.model import Lockfile
from firestop.lockfile.org import Org, Service
from firestop.lockfile.parse import UnknownLockfile, parse_file
from firestop.lockfile.writer import LockfileRows, service_rows
from firestop.schema.index import count_releases, release_index
from firestop.schema.model import (
    UPSERT_LOCKFILES,
    UPSERT_PINS,
    UPSERT_SERVICES,
    UPSERT_USES_LOCKFILE,
)

Progress = Callable[[str], None]

# How many unmatched pins to keep for the report. Enough to see the pattern,
# not enough to scroll.
_SAMPLE = 10


@dataclass(slots=True)
class LockfileStats:
    services: int = 0
    lockfiles: int = 0
    pins: int = 0
    declared: int = 0
    unmatched: int = 0
    releases_indexed: int = 0
    unmatched_sample: list[str] = field(default_factory=list)
    per_service: list[tuple[str, str, int, int]] = field(default_factory=list)


class LockfileIngest:
    def __init__(self, hydra: HydraClient, *, progress: Progress | None = None) -> None:
        self._hydra = hydra
        self._progress = progress or (lambda _message: None)
        self.stats = LockfileStats()

    async def run(self, org: Org) -> LockfileStats:
        self._progress("reading releases from the graph")
        index = await release_index(self._hydra)
        self.stats.releases_indexed = count_releases(index)

        rows = LockfileRows()
        for service in org.services:
            parsed = self._parse(service, org.root)
            if parsed is None:
                continue

            produced = service_rows(service, parsed, index)
            rows.extend(produced)

            self.stats.per_service.append(
                (service.name, str(parsed.kind), len(parsed), len(produced.pins))
            )
            self.stats.declared += len(parsed.direct)
            self._progress(
                f"{service.name}: {len(produced.pins):,} of {len(parsed):,} "
                f"pinned releases are in the graph"
            )

        self.stats.unmatched = len(rows.unknown)
        self.stats.unmatched_sample = sorted(rows.unknown)[:_SAMPLE]
        await self._write(rows)
        return self.stats

    def _parse(self, service: Service, root: Path) -> Lockfile | None:
        if service.lockfile is None:
            return None
        try:
            return parse_file(service.lockfile, relative_to=root)
        except (OSError, UnknownLockfile) as exc:
            self._progress(f"{service.name}: skipped, {exc}")
            return None

    async def _write(self, rows: LockfileRows) -> None:
        if not len(rows):
            return

        self.stats.services += await self._hydra.write_batches(UPSERT_SERVICES, rows.services)
        self.stats.lockfiles += await self._hydra.write_batches(UPSERT_LOCKFILES, rows.lockfiles)
        await self._hydra.write_batches(UPSERT_USES_LOCKFILE, rows.uses_lockfile)
        self.stats.pins += await self._hydra.write_batches(UPSERT_PINS, rows.pins)
