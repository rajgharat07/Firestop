"""Typosquat candidates near popular names, filtered by maintainer overlap."""

from __future__ import annotations

from dataclasses import dataclass, field

from firestop.hydra.client import Consistency, HydraClient
from firestop.schema.model import Label, Rel

# A package with this many dependents is established enough to be a target rather
# than a squatter.
POPULAR_AT = 25

_PACKAGES = (
    f"MATCH (p:{Label.PACKAGE}) "
    "RETURN p.name AS name, p.dependent_count AS dependents, p.latest_at AS latest_at"
)

_OWNERSHIP = (
    f"MATCH (m:{Label.MAINTAINER})-[:{Rel.CAN_PUBLISH}]->(p:{Label.PACKAGE}) "
    "RETURN p.name AS package, m.username AS username"
)


@dataclass(frozen=True, slots=True)
class Suspect:
    """A name close enough to a popular one to be worth a look."""

    name: str
    resembles: str
    distance: int
    dependents: int
    target_dependents: int
    shares_maintainer: bool
    trick: str

    @property
    def ratio(self) -> float:
        """How much smaller the suspect is than what it resembles."""
        return self.dependents / self.target_dependents if self.target_dependents else 0.0


@dataclass(slots=True)
class Radar:
    popular: int = 0
    examined: int = 0
    suspects: list[Suspect] = field(default_factory=list)

    @property
    def found(self) -> int:
        return len(self.suspects)


async def scan(
    client: HydraClient,
    *,
    popular_at: int = POPULAR_AT,
    max_distance: int = 1,
    limit: int = 50,
    consistency: Consistency | None = None,
) -> Radar:
    result = await client.run(_PACKAGES, consistency=consistency)

    dependents: dict[str, int] = {}
    for row in result.rows:
        name = str(row.get("name") or "")
        if name:
            dependents[name] = int(row.get("dependents") or 0)

    popular = {name for name, count in dependents.items() if count >= popular_at}
    radar = Radar(popular=len(popular), examined=len(dependents))
    if not popular:
        return radar

    owners = await _owners(client, consistency=consistency)
    suspects: list[Suspect] = []

    for name, count in dependents.items():
        if name in popular:
            continue
        for target in popular:
            if count >= dependents[target]:
                continue
            trick, distance = _resemblance(name, target, max_distance)
            if not trick:
                continue
            suspects.append(
                Suspect(
                    name=name,
                    resembles=target,
                    distance=distance,
                    dependents=count,
                    target_dependents=dependents[target],
                    shares_maintainer=bool(owners.get(name, set()) & owners.get(target, set())),
                    trick=trick,
                )
            )

    # Unrelated ownership first, then the widest popularity gap: that ordering
    # puts the ones worth a human's attention at the top.
    radar.suspects = sorted(
        suspects,
        key=lambda suspect: (suspect.shares_maintainer, suspect.ratio, suspect.name),
    )[:limit]
    return radar


async def _owners(client: HydraClient, *, consistency: Consistency | None) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    async for row in client.stream(_OWNERSHIP, consistency=consistency):
        package = str(row.get("package") or "")
        username = str(row.get("username") or "")
        if package and username:
            owners.setdefault(package, set()).add(username)
    return owners


def _resemblance(name: str, target: str, max_distance: int) -> tuple[str, int]:
    """How `name` imitates `target`, if it does."""
    if name == target:
        return "", 0

    # A scoped copy of an unscoped name: @types-like squatting on the real thing.
    if name.startswith("@") and name.rpartition("/")[2] == target:
        return "scoped copy", 0

    stripped = name.replace("-", "").replace("_", "").replace(".", "")
    target_stripped = target.replace("-", "").replace("_", "").replace(".", "")
    if stripped == target_stripped:
        return "punctuation", 0

    distance = _edit_distance(name, target, max_distance)
    if distance <= max_distance:
        return "one edit" if distance == 1 else f"{distance} edits", distance

    return "", 0


def _edit_distance(left: str, right: str, ceiling: int) -> int:
    """Damerau-Levenshtein, abandoned once it exceeds the ceiling.

    Transpositions count as one edit because swapping two letters is the typo
    people actually make.
    """
    if abs(len(left) - len(right)) > ceiling:
        return ceiling + 1

    previous: list[int] = []
    current = list(range(len(right) + 1))

    for i, left_character in enumerate(left, start=1):
        before, previous = previous, current
        current = [i] + [0] * len(right)
        best = current[0]

        for j, right_character in enumerate(right, start=1):
            cost = 0 if left_character == right_character else 1
            current[j] = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            if (
                i > 1
                and j > 1
                and left_character == right[j - 2]
                and left[i - 2] == right_character
            ):
                current[j] = min(current[j], before[j - 2] + 1)
            best = min(best, current[j])

        if best > ceiling:
            return ceiling + 1

    return current[-1]
