"""Eval baselines: per-service MSpaths (reference) and name-similarity ablation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from firestop.hydra.client import Consistency, HydraClient
from firestop.hydra.errors import HydraQueryError
from firestop.query import paths
from firestop.query.compromise import Compromise
from firestop.query.temporal import path_window
from firestop.query.typosquat import _edit_distance
from firestop.schema.model import BUILD_RELS, Label


@dataclass(slots=True)
class Walk:
    """What a per-service traversal found, and what it cost."""

    services: set[str] = field(default_factory=set)
    releases: set[str] = field(default_factory=set)
    round_trips: int = 0
    depth: int = 0
    elapsed_ms: float = 0.0


async def traversal_by_hand(
    client: HydraClient,
    compromise: Compromise,
    *,
    max_depth: int = 8,
    consistency: Consistency | None = None,
) -> Walk:
    """One `algo.MSpaths` call per service -- correct, just not batched."""
    started = time.perf_counter()
    walk = Walk()

    if not compromise.keys:
        walk.elapsed_ms = (time.perf_counter() - started) * 1000
        return walk

    listed = await client.run(
        f"MATCH (s:{Label.SERVICE}) RETURN s.name AS name",
        consistency=consistency,
    )
    walk.round_trips += 1
    names = sorted({str(row["name"]) for row in listed.rows if row.get("name")})

    rel_types = tuple(str(rel) for rel in BUILD_RELS)
    for name in names:
        query = paths.PathQuery(
            source=paths.Endpoint(str(Label.SERVICE), "name", (name,)),
            target=paths.Endpoint(str(Label.RELEASE), "key", compromise.keys),
            rel_types=rel_types,
            direction="outgoing",
            max_len=max_depth,
            path_count=500,
        )
        try:
            found = await paths.run(client, query, consistency=consistency)
        except HydraQueryError:
            # A hub package that refuses even a single-service frontier is not a
            # fair speed comparison; leave that service unmarked.
            walk.round_trips += 1
            continue

        walk.round_trips += max(found.round_trips, 1)
        for path in found:
            if path_window(path).empty:
                continue
            walk.services.add(name)
            walk.depth = max(walk.depth, path.length)
            for node in path.nodes:
                key = node.get("key")
                if isinstance(key, str) and key:
                    walk.releases.add(key)

    walk.elapsed_ms = (time.perf_counter() - started) * 1000
    return walk


@dataclass(slots=True)
class Ranking:
    """What name similarity thinks is related."""

    packages: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


async def by_name_similarity(
    client: HydraClient,
    compromise: Compromise,
    *,
    limit: int = 25,
    consistency: Consistency | None = None,
) -> Ranking:
    started = time.perf_counter()
    result = await client.run(
        f"MATCH (p:{Label.PACKAGE}) RETURN p.name AS name", consistency=consistency
    )

    scored: list[tuple[int, str]] = []
    for row in result.rows:
        name = str(row.get("name") or "")
        if not name or name in compromise.packages:
            continue
        distance = min(_edit_distance(name, target, 12) for target in compromise.packages)
        scored.append((distance, name))

    scored.sort()
    return Ranking(
        packages=[name for _distance, name in scored[:limit]],
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
