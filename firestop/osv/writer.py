"""Building graph rows from parsed advisories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from firestop.ids import advisory_id, edge_id
from firestop.osv.advisory import Advisory
from firestop.osv.match import Match, matches
from firestop.schema.model import Rel

# Package name to the release ids in the graph, keyed by version. Advisories are
# matched against what was actually crawled, not against the whole registry.
ReleaseIndex = Mapping[str, Mapping[str, int]]


@dataclass(slots=True)
class AdvisoryRows:
    advisories: list[dict] = field(default_factory=list)
    affects: list[dict] = field(default_factory=list)

    def extend(self, other: AdvisoryRows) -> None:
        self.advisories.extend(other.advisories)
        self.affects.extend(other.affects)

    def __len__(self) -> int:
        return len(self.advisories) + len(self.affects)


def advisory_rows(advisory: Advisory, releases: ReleaseIndex) -> AdvisoryRows:
    """Rows for one advisory, or nothing if it touches no release in the graph.

    An advisory with no edges is not worth storing. There are tens of thousands
    of npm advisories and most concern packages this graph has never seen, so
    writing them all would bury the ones that matter under noise.
    """
    rows = AdvisoryRows()
    vertex = advisory_id(advisory.osv_id)
    seen: set[int] = set()

    for affected in advisory.affected:
        known = releases.get(affected.name)
        if not known:
            continue

        for match in matches(affected, known.keys()):
            release = known[match.version]
            edge = _affects(vertex, release, match)
            # The same release can appear under two entries of one advisory, and
            # an advisory-to-release edge means the same thing whichever entry it
            # came from.
            if edge["id"] in seen:
                continue
            seen.add(edge["id"])
            rows.affects.append(edge)

    if rows.affects:
        rows.advisories.append(
            {
                "id": vertex,
                "osv_id": advisory.osv_id,
                "severity": advisory.severity,
                "published_at": advisory.published_at,
                "summary": advisory.summary,
                "cwe": advisory.cwe,
                "aliases": advisory.aliases,
            }
        )

    return rows


def _affects(advisory: int, release: int, match: Match) -> dict:
    return {
        "id": edge_id(str(Rel.AFFECTS), advisory, release),
        "source": advisory,
        "target": release,
        "introduced": match.introduced,
        "fixed_in": match.fixed_in,
        "range_source": match.source,
    }
