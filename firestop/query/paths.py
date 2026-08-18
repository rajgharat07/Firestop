"""Bounded multi-source/target traversal via HydraDB `algo.MSpaths`.

Config must be inline literals; selectors are string lists (hence Release.key).
`pathCount` defaults to 1 if omitted. Chunks stay under the ~1 MiB body cap.
On `resource_exhausted`, retry with a shorter maxLen rather than sleeping.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from firestop.hydra.client import Consistency, HydraClient
from firestop.hydra.errors import HydraQueryError
from firestop.hydra.values import GraphPath

Direction = Literal["outgoing", "incoming"]

# Body budget for selector lists; rest of the request still has to fit in 1 MiB.
_MAX_SELECTOR_BYTES = 256 * 1024

# Service -> lockfile -> pin is two hops; need at least one more for a dep.
_MIN_DEPTH = 3

_TOO_BIG = "resource_exhausted"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One end of a traversal: a label, the property to match, and the values."""

    label: str
    property: str
    values: tuple[str, ...] = ()

    def with_values(self, values: Iterable[str]) -> Endpoint:
        return Endpoint(self.label, self.property, tuple(values))


@dataclass(frozen=True, slots=True)
class PathQuery:
    source: Endpoint
    rel_types: tuple[str, ...]
    target: Endpoint | None = None
    direction: Direction = "outgoing"
    max_len: int = 8
    path_count: int = 2000
    extra: dict[str, str] = field(default_factory=dict)

    def cypher(self) -> str:
        config = [
            f"sourceLabel: {_literal(self.source.label)}",
            f"sourceProperty: {_literal(self.source.property)}",
            f"sourceValues: {_literal_list(self.source.values)}",
        ]
        if self.target is not None:
            config += [
                f"targetLabel: {_literal(self.target.label)}",
                f"targetProperty: {_literal(self.target.property)}",
                f"targetValues: {_literal_list(self.target.values)}",
            ]
        config += [
            f"relTypes: {_literal_list(self.rel_types)}",
            f"relDirection: {_literal(self.direction)}",
            f"maxLen: {int(self.max_len)}",
            f"pathCount: {int(self.path_count)}",
        ]
        config += [f"{name}: {value}" for name, value in self.extra.items()]

        return f"CALL algo.MSpaths({{{', '.join(config)}}}) YIELD path RETURN path"


@dataclass(slots=True)
class Traversal:
    """Paths, and how deep the node was actually willing to look for them."""

    paths: list[GraphPath] = field(default_factory=list)
    depth: int = 0
    asked_for: int = 0
    round_trips: int = 0

    @property
    def shortened(self) -> bool:
        """True when the answer is bounded by admission control, not the graph."""
        return self.depth < self.asked_for

    def __len__(self) -> int:
        return len(self.paths)

    def __iter__(self):
        return iter(self.paths)


async def run(
    client: HydraClient,
    query: PathQuery,
    *,
    consistency: Consistency | None = None,
) -> Traversal:
    """Execute a traversal, splitting the selector lists to fit the body limit."""
    found = Traversal(depth=query.max_len, asked_for=query.max_len)

    for chunk in _chunks(query.source.values):
        depth = found.depth

        while True:
            call = PathQuery(
                source=query.source.with_values(chunk),
                rel_types=query.rel_types,
                target=query.target,
                direction=query.direction,
                max_len=depth,
                path_count=query.path_count,
                extra=query.extra,
            )
            try:
                result = await client.run(call.cypher(), consistency=consistency)
            except HydraQueryError as exc:
                if exc.code != _TOO_BIG or depth <= _MIN_DEPTH:
                    raise
                depth -= 1
                found.round_trips += 1
                continue

            found.round_trips += 1
            found.paths.extend(
                row["path"] for row in result.rows if isinstance(row.get("path"), GraphPath)
            )
            break

        # Later chunks start at the depth that worked, so one hub package does
        # not cost a fresh descent per chunk, and the answer as a whole is
        # reported at the shallowest bound any part of it needed.
        found.depth = depth

    return found


def _chunks(values: Sequence[str]) -> list[tuple[str, ...]]:
    if not values:
        return [()]

    chunks: list[tuple[str, ...]] = []
    current: list[str] = []
    size = 0

    for value in values:
        cost = len(value) + 4
        if current and size + cost > _MAX_SELECTOR_BYTES:
            chunks.append(tuple(current))
            current, size = [], 0
        current.append(value)
        size += cost

    if current:
        chunks.append(tuple(current))
    return chunks


def _literal(value: str) -> str:
    """A single-quoted Cypher string literal.

    The config map cannot be parameterised, so values reach the parser as text.
    npm names cannot contain a quote, but the selectors also carry advisory ids
    and service names from a manifest, and "cannot" is not a security control.
    """
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _literal_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(_literal(value) for value in values) + "]"
