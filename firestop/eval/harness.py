"""Score Firestop blast against per-service MSpaths and a name-similarity ablation."""

from __future__ import annotations

from dataclasses import dataclass, field

from firestop.eval.baseline import by_name_similarity, traversal_by_hand
from firestop.hydra.client import Consistency, HydraClient
from firestop.query import blast as blast_query
from firestop.query.compromise import Compromise, UnknownCompromise, from_advisory

_INCIDENTS = (
    "MATCH (a:Advisory)-[:AFFECTS]->(r:Release) "
    "RETURN a.osv_id AS osv_id, a.severity AS severity "
    "ORDER BY a.published_at DESC LIMIT $limit"
)


@dataclass(slots=True)
class Score:
    matched: int = 0
    missed: int = 0
    invented: int = 0

    @property
    def precision(self) -> float:
        found = self.matched + self.invented
        return self.matched / found if found else 1.0

    @property
    def recall(self) -> float:
        expected = self.matched + self.missed
        return self.matched / expected if expected else 1.0

    @property
    def exact(self) -> bool:
        return not self.missed and not self.invented

    def against(self, expected: set[str], found: set[str]) -> Score:
        self.matched = len(expected & found)
        self.missed = len(expected - found)
        self.invented = len(found - expected)
        return self


@dataclass(slots=True)
class Case:
    advisory: str
    severity: str = ""
    exposed: int = 0
    firestop_ms: float = 0.0
    baseline_ms: float = 0.0
    round_trips: int = 0
    similarity_ms: float = 0.0
    agrees: bool = True
    similarity: Score = field(default_factory=Score)

    @property
    def speedup(self) -> float:
        return self.baseline_ms / self.firestop_ms if self.firestop_ms else 0.0


@dataclass(slots=True)
class Report:
    cases: list[Case] = field(default_factory=list)
    skipped: int = 0

    @property
    def agreement(self) -> float:
        if not self.cases:
            return 1.0
        return sum(1 for case in self.cases if case.agrees) / len(self.cases)

    @property
    def median_speedup(self) -> float:
        if not self.cases:
            return 0.0
        ordered = sorted(case.speedup for case in self.cases)
        return ordered[len(ordered) // 2]

    @property
    def round_trips(self) -> int:
        return sum(case.round_trips for case in self.cases)

    @property
    def similarity_precision(self) -> float:
        scored = [case.similarity for case in self.cases if case.exposed]
        if not scored:
            return 0.0
        return sum(score.precision for score in scored) / len(scored)

    @property
    def similarity_recall(self) -> float:
        scored = [case.similarity for case in self.cases if case.exposed]
        if not scored:
            return 0.0
        return sum(score.recall for score in scored) / len(scored)


async def evaluate(
    client: HydraClient,
    *,
    limit: int = 20,
    consistency: Consistency | None = None,
) -> Report:
    """Run every recent advisory that touches the graph through both approaches."""
    report = Report()

    advisories = await client.run(_INCIDENTS, {"limit": limit * 4}, consistency=consistency)
    seen: set[str] = set()

    for row in advisories.rows:
        osv_id = str(row.get("osv_id") or "")
        if not osv_id or osv_id in seen:
            continue
        seen.add(osv_id)

        try:
            compromise = await from_advisory(client, osv_id, consistency=consistency)
        except UnknownCompromise:
            report.skipped += 1
            continue

        report.cases.append(await _case(client, compromise, consistency=consistency))
        if len(report.cases) >= limit:
            break

    return report


async def _case(
    client: HydraClient, compromise: Compromise, *, consistency: Consistency | None
) -> Case:
    case = Case(advisory=compromise.advisory, severity=compromise.severity)

    radius = await blast_query.exposure(client, compromise, consistency=consistency)
    case.firestop_ms = radius.elapsed_ms
    case.exposed = radius.exposed
    ours = {service.service for service in radius.services}

    walk = await traversal_by_hand(client, compromise, consistency=consistency)
    case.baseline_ms = walk.elapsed_ms
    case.round_trips = walk.round_trips
    case.agrees = ours == walk.services

    ranking = await by_name_similarity(client, compromise, consistency=consistency)
    case.similarity_ms = ranking.elapsed_ms
    # Similarity ranks package names, so it is scored on the packages the walk
    # actually implicated -- the most generous reading of what it was trying to
    # do -- rather than on services it never had a way to name.
    case.similarity.against(_packages(walk.releases), set(ranking.packages))

    return case


def _packages(keys: set[str]) -> set[str]:
    """Package names behind a set of `name@version` keys."""
    names = set()
    for key in keys:
        name = key[1:].rpartition("@")[0] if key.startswith("@") else key.rpartition("@")[0]
        if name:
            names.add(f"@{name}" if key.startswith("@") else name)
    return names
