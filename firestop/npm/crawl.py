"""Two-pass npm ingest: discover packages, then link temporal DEPENDS_ON.

Linking needs each dependency's full publish timeline, so discovery writes
vertices first and a second pass resolves ranges into validity windows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import orjson

from firestop.hydra.client import HydraClient
from firestop.ids import release_id
from firestop.npm.packument import Packument
from firestop.npm.registry import RegistryClient
from firestop.npm.resolve import Resolver
from firestop.npm.seeds import default_seeds
from firestop.npm.writer import (
    VertexRows,
    collect_dependents,
    dependency_rows,
    dependent_count_rows,
    vertex_rows,
)
from firestop.schema.model import (
    UPDATE_DEPENDENT_COUNT,
    UPSERT_CAN_PUBLISH,
    UPSERT_DEPENDS_ON,
    UPSERT_DEV_DEPENDS_ON,
    UPSERT_MAINTAINERS,
    UPSERT_PACKAGES,
    UPSERT_PUBLISHED,
    UPSERT_RELEASES,
    UPSERT_VERSION_OF,
    DependencyKind,
    Rel,
)

_STATE_VERSION = 3
# Older files carry a subset of these fields, so they still resume; they just
# start the link pass over.
_READABLE_STATE_VERSIONS = frozenset({2, 3})
_FLUSH_AT = 4000

# Depth from seed at which we still write/follow DEV edges. 0 = seeds only
# (npm installs a package's own devDeps, not transitive ones).
DEFAULT_DEV_HORIZON = 0

_TRANSITIVE_KINDS = frozenset({DependencyKind.RUNTIME, DependencyKind.OPTIONAL})
_DIRECT_KINDS = _TRANSITIVE_KINDS | {DependencyKind.DEV, DependencyKind.PEER}

Progress = Callable[[str], None]


@dataclass(slots=True)
class CrawlStats:
    packages: int = 0
    releases: int = 0
    maintainers: int = 0
    depends_on: int = 0
    dev_depends_on: int = 0
    fetched: int = 0
    cache_hits: int = 0
    missing: int = 0
    failed: int = 0
    unresolvable_ranges: int = 0
    unsatisfied_ranges: int = 0
    unwritten_targets: int = 0


@dataclass(slots=True)
class CrawlState:
    """Checkpoint for resumable crawl (frontier + link progress)."""

    visited: set[str] = field(default_factory=set)
    frontier: list[tuple[str, int]] = field(default_factory=list)
    enqueued: set[str] = field(default_factory=set)
    # Depth from seed — gates whether DEV edges are recorded.
    depths: dict[str, int] = field(default_factory=dict)
    linked: bool = False
    linked_packages: set[str] = field(default_factory=set)
    # Dependents seen so far, carried across runs because they are counted per
    # package and one package's edges can straddle a crash.
    dependents: dict[str, set[str]] = field(default_factory=dict)

    def seed(self, names: Iterable[str]) -> None:
        for name in names:
            if name not in self.enqueued:
                self.enqueued.add(name)
                self.frontier.append((name, 0))

    def enqueue(self, names: Iterable[str], depth: int) -> None:
        for name in names:
            if name not in self.enqueued:
                self.enqueued.add(name)
                self.frontier.append((name, depth))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = orjson.dumps(
            {
                "state_version": _STATE_VERSION,
                "visited": sorted(self.visited),
                "frontier": [list(entry) for entry in self.frontier],
                "enqueued": sorted(self.enqueued),
                "depths": self.depths,
                "linked": self.linked,
                "linked_packages": sorted(self.linked_packages),
                "dependents": {name: sorted(names) for name, names in self.dependents.items()},
            }
        )
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> CrawlState | None:
        if not path.exists():
            return None
        try:
            raw = orjson.loads(path.read_bytes())
        except (OSError, orjson.JSONDecodeError):
            return None
        if raw.get("state_version") not in _READABLE_STATE_VERSIONS:
            return None
        return cls(
            visited=set(raw.get("visited") or []),
            frontier=[(str(n), int(d)) for n, d in raw.get("frontier") or []],
            enqueued=set(raw.get("enqueued") or []),
            depths={str(name): int(depth) for name, depth in (raw.get("depths") or {}).items()},
            linked=bool(raw.get("linked")),
            linked_packages=set(raw.get("linked_packages") or []),
            dependents={
                str(name): set(names) for name, names in (raw.get("dependents") or {}).items()
            },
        )


class Crawler:
    def __init__(
        self,
        hydra: HydraClient,
        registry: RegistryClient,
        *,
        state_path: Path = Path("data/crawl-state.json"),
        max_packages: int = 2000,
        max_depth: int = 4,
        dev_horizon: int = DEFAULT_DEV_HORIZON,
        resolver: Resolver | None = None,
        progress: Progress | None = None,
    ) -> None:
        self._hydra = hydra
        self._registry = registry
        self._state_path = state_path
        self._max_packages = max_packages
        self._max_depth = max_depth
        self._dev_horizon = dev_horizon
        self._resolver = resolver or Resolver()
        self._progress = progress or (lambda _message: None)
        self.stats = CrawlStats()

    async def run(self, seeds: Iterable[str] | None = None, *, resume: bool = True) -> CrawlStats:
        state = CrawlState.load(self._state_path) if resume else None
        if state is None:
            state = CrawlState()
            state.seed(seeds if seeds is not None else default_seeds())

        await self.discover(state)
        await self.link(state)
        return self.stats

    async def discover(self, state: CrawlState) -> None:
        """Breadth-first walk, writing everything that does not need resolution."""
        buffer = VertexRows()

        while state.frontier and len(state.visited) < self._max_packages:
            level = self._take_level(state)
            if not level:
                break

            depth = level[0][1]
            names = [name for name, _ in level]
            self._progress(
                f"depth {depth}: fetching {len(names)} packages "
                f"({len(state.visited)}/{self._max_packages} done)"
            )

            for packument in await self._fetch_all(names):
                state.visited.add(packument.name)
                state.depths.setdefault(packument.name, depth)
                buffer.extend(vertex_rows(packument))

                if depth < self._max_depth:
                    kinds = _DIRECT_KINDS if depth == 0 else _TRANSITIVE_KINDS
                    state.enqueue(_dependencies_to_follow(packument, kinds), depth + 1)

                if len(buffer) >= _FLUSH_AT:
                    await self._flush_vertices(buffer)
                    buffer = VertexRows()
                    state.save(self._state_path)

            await self._flush_vertices(buffer)
            buffer = VertexRows()
            state.save(self._state_path)

        self._record_registry_stats()

    async def link(self, state: CrawlState) -> None:
        """Resolve declared ranges into `DEPENDS_ON` edges."""
        names = sorted(state.visited)
        self._progress(f"indexing publish timelines for {len(names)} packages")

        # Only the timelines are held in memory. Packuments are streamed one at a
        # time below, because holding thousands of them at once is unnecessary.
        timelines: dict[str, dict[str, int]] = {}
        release_owner: dict[int, str] = {}
        for name in names:
            packument = self._registry.load_cached(name)
            if packument is None:
                continue
            timelines[name] = packument.version_times
            for release in packument.releases:
                release_owner[release_id(name, release.version)] = name

        remaining = [name for name in names if name not in state.linked_packages]
        if len(remaining) < len(names):
            self._progress(f"resuming: {len(names) - len(remaining):,} packages already linked")

        edges: dict[Rel, list[dict]] = {Rel.DEPENDS_ON: [], Rel.DEV_DEPENDS_ON: []}
        batch: list[str] = []
        pending = 0

        for name in remaining:
            packument = self._registry.load_cached(name)
            if packument is None:
                state.linked_packages.add(name)
                continue

            for relationship, rows in dependency_rows(
                packument,
                timelines,
                self._resolver,
                release_owner,
                with_dev=state.depths.get(name, self._dev_horizon + 1) <= self._dev_horizon,
            ).items():
                edges[relationship].extend(rows)
                pending += len(rows)
            batch.append(name)

            if pending >= _FLUSH_AT:
                await self._commit_edges(edges, release_owner, state, batch)
                batch, pending = [], 0
                self._progress(
                    f"linked {len(state.linked_packages)}/{len(names)} packages, "
                    f"{self.stats.depends_on:,} installable and "
                    f"{self.stats.dev_depends_on:,} build-time edges"
                )

        await self._commit_edges(edges, release_owner, state, batch)

        if state.dependents:
            self._progress(f"updating dependent counts for {len(state.dependents):,} packages")
            await self._hydra.write_batches(
                UPDATE_DEPENDENT_COUNT, dependent_count_rows(state.dependents)
            )

        state.linked = True
        state.save(self._state_path)
        self._record_registry_stats()

    async def _commit_edges(
        self,
        edges: dict[Rel, list[dict]],
        release_owner: dict[int, str],
        state: CrawlState,
        batch: list[str],
    ) -> None:
        """Write one flush of edges and record that those packages are done.

        The checkpoint is written after the edges commit, never before: a package
        marked linked whose edges were lost would leave a hole no later run fills.
        """
        await self._flush_edges(edges, release_owner, state.dependents)
        state.linked_packages.update(batch)
        state.save(self._state_path)

    def _take_level(self, state: CrawlState) -> list[tuple[str, int]]:
        """Pop every queued package at the shallowest remaining depth."""
        if not state.frontier:
            return []

        depth = state.frontier[0][1]
        remaining = self._max_packages - len(state.visited)
        level: list[tuple[str, int]] = []

        while state.frontier and state.frontier[0][1] == depth and len(level) < remaining:
            name, entry_depth = state.frontier.pop(0)
            if name not in state.visited:
                level.append((name, entry_depth))

        return level

    async def _fetch_all(self, names: list[str]) -> list[Packument]:
        semaphore = asyncio.Semaphore(self._hydra.settings.crawl_concurrency)

        async def fetch(name: str) -> Packument | None:
            async with semaphore:
                return await self._registry.packument(name)

        results = await asyncio.gather(*(fetch(name) for name in names), return_exceptions=True)
        packuments: list[Packument] = []
        for result in results:
            if isinstance(result, Packument):
                packuments.append(result)
            elif isinstance(result, BaseException):
                self.stats.failed += 1
        return packuments

    async def _flush_edges(
        self,
        edges: dict[Rel, list[dict]],
        release_owner: dict[int, str],
        dependents: dict[str, set[str]],
    ) -> None:
        statements = {
            Rel.DEPENDS_ON: UPSERT_DEPENDS_ON,
            Rel.DEV_DEPENDS_ON: UPSERT_DEV_DEPENDS_ON,
        }
        for relationship, rows in edges.items():
            if not rows:
                continue
            # Dev edges count towards dependent totals too: a compromised build
            # tool still lands on everyone who installs it.
            collect_dependents(rows, release_owner, dependents)
            written = await self._hydra.write_batches(statements[relationship], rows)
            if relationship is Rel.DEPENDS_ON:
                self.stats.depends_on += written
            else:
                self.stats.dev_depends_on += written
            rows.clear()

    async def _flush_vertices(self, rows: VertexRows) -> None:
        if not len(rows):
            return

        # Order matters: relationship upserts MATCH both endpoints, so vertices
        # have to exist first or the edge silently matches nothing.
        self.stats.packages += await self._hydra.write_batches(
            UPSERT_PACKAGES, _unique(rows.packages)
        )
        self.stats.releases += await self._hydra.write_batches(
            UPSERT_RELEASES, _unique(rows.releases)
        )
        self.stats.maintainers += await self._hydra.write_batches(
            UPSERT_MAINTAINERS, _unique(rows.maintainers)
        )
        await self._hydra.write_batches(UPSERT_VERSION_OF, _unique(rows.version_of))
        await self._hydra.write_batches(UPSERT_CAN_PUBLISH, _unique(rows.can_publish))
        await self._hydra.write_batches(UPSERT_PUBLISHED, _unique(rows.published))

    def _record_registry_stats(self) -> None:
        self.stats.fetched = self._registry.fetched
        self.stats.cache_hits = self._registry.cache_hits
        self.stats.missing = self._registry.missing
        self.stats.failed = self._registry.failed
        self.stats.unresolvable_ranges = self._resolver.unresolvable_ranges
        self.stats.unsatisfied_ranges = self._resolver.unsatisfied_ranges
        self.stats.unwritten_targets = self._resolver.unwritten_targets


def _dependencies_to_follow(
    packument: Packument, kinds: frozenset[DependencyKind] | set[DependencyKind]
) -> set[str]:
    return {
        dependency.name
        for release in packument.releases
        for dependency in release.dependencies
        if dependency.kind in kinds
    }


def _unique(rows: list[dict]) -> list[dict]:
    """Collapse rows that share an id.

    One package's releases can name the same maintainer many times, and a batch
    that upserts the same id twice does redundant work for no benefit.
    """
    seen: dict[int, dict] = {}
    for row in rows:
        seen[row["id"]] = row
    return list(seen.values())
