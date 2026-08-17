"""Stream crawled releases from Hydra, match OSV, write AFFECTS."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from firestop.hydra.client import HydraClient
from firestop.osv.advisory import parse_advisory
from firestop.osv.bulk import BulkExport
from firestop.osv.writer import AdvisoryRows, advisory_rows
from firestop.schema.index import ReleaseIndex, count_releases, release_index
from firestop.schema.model import UPSERT_ADVISORIES, UPSERT_AFFECTS

_FLUSH_AT = 4000

Progress = Callable[[str], None]


@dataclass(slots=True)
class AdvisoryStats:
    records: int = 0
    parsed: int = 0
    advisories: int = 0
    affects: int = 0
    # Advisories that parsed cleanly but named no package in the graph. Expected,
    # and worth showing: it is the difference between "npm has a problem" and
    # "this graph has a problem".
    out_of_scope: int = 0
    releases_indexed: int = 0
    packages_indexed: int = 0
    malformed: int = 0
    downloaded_bytes: int = 0


class AdvisoryIngest:
    def __init__(
        self,
        hydra: HydraClient,
        bulk: BulkExport,
        *,
        progress: Progress | None = None,
    ) -> None:
        self._hydra = hydra
        self._bulk = bulk
        self._progress = progress or (lambda _message: None)
        self.stats = AdvisoryStats()

    async def run(self, *, refresh: bool = False) -> AdvisoryStats:
        if not self._bulk.is_cached or refresh:
            self._progress("downloading the OSV npm export")
        await self._bulk.download(refresh=refresh)
        self.stats.downloaded_bytes = self._bulk.downloaded_bytes

        index = await self._load_index()
        if not index:
            self._progress("graph has no releases yet, so nothing can be matched")
            return self.stats

        await self._ingest(index)
        self.stats.malformed = self._bulk.malformed
        return self.stats

    async def _load_index(self) -> ReleaseIndex:
        """Every release in the graph, grouped by package name."""
        self._progress("reading releases from the graph")
        index = await release_index(self._hydra)

        self.stats.packages_indexed = len(index)
        self.stats.releases_indexed = count_releases(index)
        self._progress(
            f"indexed {self.stats.releases_indexed:,} releases "
            f"across {self.stats.packages_indexed:,} packages"
        )
        return index

    async def _ingest(self, index: ReleaseIndex) -> None:
        buffer = AdvisoryRows()

        for document in self._bulk.records():
            self.stats.records += 1

            advisory = parse_advisory(document)
            if advisory is None:
                continue
            self.stats.parsed += 1

            if not advisory.package_names & index.keys():
                self.stats.out_of_scope += 1
                continue

            rows = advisory_rows(advisory, index)
            if not rows.advisories:
                self.stats.out_of_scope += 1
                continue

            buffer.extend(rows)
            if len(buffer) >= _FLUSH_AT:
                await self._flush(buffer)
                buffer = AdvisoryRows()
                self._progress(
                    f"{self.stats.advisories:,} advisories, "
                    f"{self.stats.affects:,} affected releases"
                )

        await self._flush(buffer)

    async def _flush(self, rows: AdvisoryRows) -> None:
        if not len(rows):
            return

        # Vertices first: the edge upsert matches both endpoints, so an advisory
        # written after its edges would match nothing and lose them silently.
        self.stats.advisories += await self._hydra.write_batches(UPSERT_ADVISORIES, rows.advisories)
        self.stats.affects += await self._hydra.write_batches(UPSERT_AFFECTS, rows.affects)
