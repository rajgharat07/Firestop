"""Maintainer pivot: other packages the same publishers can ship."""

from __future__ import annotations

from dataclasses import dataclass, field

from firestop.hydra.client import Consistency, HydraClient
from firestop.schema.model import Label, Rel


@dataclass(frozen=True, slots=True)
class Sibling:
    """Another package the same accounts can publish."""

    package: str
    dependents: int
    maintainers: tuple[str, ...]

    @property
    def shared(self) -> int:
        return len(self.maintainers)


@dataclass(slots=True)
class Pivot:
    package: str
    maintainers: tuple[str, ...] = ()
    siblings: list[Sibling] = field(default_factory=list)

    @property
    def reach(self) -> int:
        return len(self.siblings)

    @property
    def dependents(self) -> int:
        """Everything downstream of the accounts, not just of the one package."""
        return sum(sibling.dependents for sibling in self.siblings)


async def maintainers_of(
    client: HydraClient, package: str, *, consistency: Consistency | None = None
) -> tuple[str, ...]:
    result = await client.run(
        f"MATCH (m:{Label.MAINTAINER})-[:{Rel.CAN_PUBLISH}]->(p:{Label.PACKAGE}) "
        "WHERE p.name = $package RETURN m.username AS username",
        {"package": package},
        consistency=consistency,
    )
    return tuple(sorted({str(row["username"]) for row in result.rows if row.get("username")}))


async def pivot(
    client: HydraClient,
    package: str,
    *,
    limit: int = 50,
    consistency: Consistency | None = None,
) -> Pivot:
    """Everything else the accounts behind `package` can publish."""
    accounts = await maintainers_of(client, package, consistency=consistency)
    found = Pivot(package=package, maintainers=accounts)
    if not accounts:
        return found

    # No IN operator in this OpenCypher subset, so membership is an OR chain.
    predicate = " OR ".join(f"m.username = ${name}" for name in _names(accounts))
    parameters = dict(zip(_names(accounts), accounts, strict=True))

    result = await client.run(
        f"MATCH (m:{Label.MAINTAINER})-[:{Rel.CAN_PUBLISH}]->(p:{Label.PACKAGE}) "
        f"WHERE {predicate} "
        "RETURN p.name AS package, p.dependent_count AS dependents, "
        "m.username AS username",
        parameters,
        consistency=consistency,
    )

    reachable: dict[str, tuple[int, set[str]]] = {}
    for row in result.rows:
        name = str(row.get("package") or "")
        if not name or name == package:
            continue
        dependents = row.get("dependents")
        count, accounts_seen = reachable.setdefault(name, (0, set()))
        accounts_seen.add(str(row.get("username") or ""))
        reachable[name] = (max(count, int(dependents or 0)), accounts_seen)

    found.siblings = sorted(
        (
            Sibling(package=name, dependents=count, maintainers=tuple(sorted(accounts_seen)))
            for name, (count, accounts_seen) in reachable.items()
        ),
        key=lambda sibling: (-sibling.dependents, sibling.package),
    )[:limit]

    return found


def _names(accounts: tuple[str, ...]) -> list[str]:
    return [f"account_{index}" for index in range(len(accounts))]
