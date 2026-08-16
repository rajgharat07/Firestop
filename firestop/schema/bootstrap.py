"""Doctor checks: write/read round-trip and optional census (no DDL to run)."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field

from firestop.hydra.client import HydraClient
from firestop.hydra.errors import HydraError, HydraQueryError
from firestop.ids import Kind, vertex_id
from firestop.schema.model import Label, Rel

# Well under the node's own ceiling, so a slow count comes back as "unknown"
# rather than holding up a health check for a minute.
_COUNT_TIMEOUT_MS = 15_000

# Probe vertices live on their own relationship type and carry no label, so they
# cannot be confused with real data or counted by the census.
_PROBE_REL = "FIRESTOP_PROBE"
_PROBE_SOURCE = vertex_id(Kind.PACKAGE, "__firestop_probe_source__")
_PROBE_TARGET = vertex_id(Kind.PACKAGE, "__firestop_probe_target__")


@dataclass(slots=True)
class Census:
    # None means the count did not finish in time, which is a different fact from
    # zero and must not be rendered as one.
    vertices: dict[str, int | None] = field(default_factory=dict)
    relationships: dict[str, int | None] = field(default_factory=dict)

    @property
    def total_vertices(self) -> int:
        return sum(count for count in self.vertices.values() if count)

    @property
    def total_relationships(self) -> int:
        return sum(count for count in self.relationships.values() if count)

    @property
    def counted_everything(self) -> bool:
        counts = (*self.vertices.values(), *self.relationships.values())
        return all(count is not None for count in counts)

    @property
    def is_empty(self) -> bool:
        return (
            self.counted_everything and self.total_vertices == 0 and self.total_relationships == 0
        )


@dataclass(slots=True)
class HealthReport:
    node_ready: bool = False
    indexer_ready: bool = False
    write_ok: bool = False
    read_ok: bool = False
    bolt_ok: bool | None = None
    read_epoch: int | None = None
    census: Census = field(default_factory=Census)
    errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        # The indexer is deliberately part of the bar. Without it, traversal
        # falls back to canonical adjacency and still returns correct answers,
        # so nothing looks broken -- it just gets slow enough to matter.
        return self.node_ready and self.indexer_ready and self.write_ok and self.read_ok


async def round_trip(client: HydraClient) -> tuple[bool, bool, int | None]:
    """Write a probe edge, read it back, then remove it.

    Returns whether the write committed, whether the read observed it, and the
    epoch the read was pinned to.
    """
    write_ok = read_ok = False
    read_epoch: int | None = None

    try:
        await client.run(
            f"MERGE (a {{id: $source}})-[:{_PROBE_REL}]->(b {{id: $target}})",
            {"source": _PROBE_SOURCE, "target": _PROBE_TARGET},
        )
        write_ok = True

        # `strong` refreshes from object storage before pinning a snapshot. For a
        # readiness check that is the point: a causal read could be served from a
        # view that predates the write above and prove nothing.
        result = await client.run(
            f"MATCH (a {{id: $source}})-[:{_PROBE_REL}]->(b) RETURN b.id AS id",
            {"source": _PROBE_SOURCE},
            consistency="strong",
        )
        read_epoch = result.read_epoch
        read_ok = result.scalar() == _PROBE_TARGET
    finally:
        # Cleanup is best effort; two stray unlabelled vertices do not affect any
        # query Firestop runs.
        for probe in (_PROBE_SOURCE, _PROBE_TARGET):
            with suppress(HydraError):
                await client.run("MATCH (n {id: $id}) DETACH DELETE n", {"id": probe})

    return write_ok, read_ok, read_epoch


async def census(client: HydraClient, *, relationships: bool = False) -> Census:
    """Count vertices per label, and optionally relationships per type.

    Relationship counts are opt-in because they are not cheap the way vertex
    counts are. A label scan is served from the label index, but counting
    relationships of a type means walking every edge record with nothing to
    anchor the scan, and past a few hundred thousand edges that exceeds the
    node's query timeout. Traversal from named endpoints -- which is what every
    real query here does -- stays fast regardless.
    """
    result = Census()

    for label in Label:
        result.vertices[str(label)] = await _count(
            client, f"MATCH (n:{label}) RETURN count(*) AS total"
        )

    if relationships:
        for rel in Rel:
            result.relationships[str(rel)] = await _count(
                client, f"MATCH ()-[r:{rel}]->() RETURN count(*) AS total"
            )

    return result


async def check(
    client: HydraClient, *, include_census: bool = True, count_relationships: bool = False
) -> HealthReport:
    """Full readiness report for the graph."""
    report = HealthReport()
    report.node_ready = await client.node_ready()
    report.indexer_ready = await client.indexer_ready()

    if not report.node_ready:
        report.errors.append(
            f"graph-node is not ready at {client.settings.hydradb_admin_url}/readyz"
        )
        return report

    if not report.indexer_ready:
        report.errors.append(
            "graph-indexer is not ready at "
            f"{client.settings.hydradb_indexer_admin_url}/readyz -- traversal will "
            "fall back to canonical adjacency and get slower"
        )

    try:
        report.write_ok, report.read_ok, report.read_epoch = await round_trip(client)
    except HydraError as exc:
        report.errors.append(f"round trip failed: {exc}")
        return report

    if include_census:
        try:
            report.census = await census(client, relationships=count_relationships)
        except HydraError as exc:
            report.errors.append(f"census failed: {exc}")

    return report


async def _count(client: HydraClient, query: str) -> int | None:
    """A count, or None if the node gave up on it."""
    try:
        result = await client.run(query, timeout_ms=_COUNT_TIMEOUT_MS)
    except HydraQueryError as exc:
        if exc.code == "query_timeout":
            return None
        raise
    return int(result.scalar(0) or 0)
