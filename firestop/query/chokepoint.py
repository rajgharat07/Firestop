"""Min hitting-set over blast paths: fewest bumps that cut every live path.

Greedy first, then exact branch-and-bound when the hop set is small enough.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cmp_to_key
from itertools import combinations

import nodesemver as semver

from firestop.hydra.client import Consistency, HydraClient
from firestop.query import paths
from firestop.query.blast import BlastRadius, ExposurePath, Link
from firestop.query.compromise import Compromise
from firestop.schema.model import Label, Rel

# Above this, the exact search is abandoned for the greedy cover. Chosen so the
# worst case stays well inside a second.
_EXACT_CANDIDATE_LIMIT = 40
_EXACT_DEPTH_LIMIT = 4


@dataclass(frozen=True, slots=True)
class Chokepoint:
    """One hop that several exposure paths pass through."""

    link: Link
    paths: frozenset[int]

    @property
    def severs(self) -> int:
        return len(self.paths)

    @property
    def mine(self) -> bool:
        return self.link.mine

    def describe(self) -> str:
        if self.link.mine:
            return f"unpin {self.link.depends_on} in {self.link.holder}"
        return f"stop {self.link.holder} depending on {self.link.depends_on}"


@dataclass(slots=True)
class Remediation:
    """What to change, and what it buys."""

    cuts: tuple[Chokepoint, ...] = ()
    paths: int = 0
    severed: int = 0
    exact: bool = True
    # What the obvious approach would have cost: one change per exposed service
    # per compromised package.
    naive: int = 0

    @property
    def complete(self) -> bool:
        return self.paths > 0 and self.severed == self.paths

    @property
    def mine(self) -> tuple[Chokepoint, ...]:
        return tuple(cut for cut in self.cuts if cut.mine)

    @property
    def upstream(self) -> tuple[Chokepoint, ...]:
        return tuple(cut for cut in self.cuts if not cut.mine)

    @property
    def saved(self) -> int:
        return max(self.naive - len(self.cuts), 0)


def remediate(radius: BlastRadius, *, avoid: Iterable[Link] = ()) -> Remediation:
    """The fewest changes that cut every live exposure path in a blast radius.

    `avoid` drops hops already found to have nowhere to upgrade to, so a second
    pass routes around them instead of repeating advice that cannot be followed.
    """
    paths = [path for service in radius.services for path in service.paths]
    if not paths:
        return Remediation()

    banned = frozenset(avoid)
    candidates = [cut for cut in _candidates(paths) if cut.link not in banned]
    if not candidates:
        return Remediation(paths=len(paths), naive=_naive(radius))
    cover = _cover(candidates, len(paths))
    exact = len(candidates) <= _EXACT_CANDIDATE_LIMIT

    if exact:
        better = _exact_cover(candidates, len(paths), ceiling=len(cover))
        if better is not None:
            cover = better

    severed = len({index for cut in cover for index in cut.paths})
    return Remediation(
        cuts=tuple(cover),
        paths=len(paths),
        severed=severed,
        exact=exact,
        naive=_naive(radius),
    )


def _candidates(paths: Sequence[ExposurePath]) -> list[Chokepoint]:
    """Every actionable hop, with the paths it appears on.

    Hops are identified by what they mean rather than by edge id, so the same
    dependency appearing on ten paths is one candidate rather than ten.
    """
    seen: dict[Link, set[int]] = {}

    for index, path in enumerate(paths):
        for link in path.links:
            if link.actionable:
                seen.setdefault(link, set()).add(index)

    return sorted(
        (Chokepoint(link=link, paths=frozenset(indices)) for link, indices in seen.items()),
        # Prefer wider cuts; prefer pins we own.
        key=lambda cut: (-cut.severs, not cut.mine, cut.link.depends_on),
    )


def _cover(candidates: Sequence[Chokepoint], total: int) -> list[Chokepoint]:
    """Greedy set cover over remaining paths."""
    remaining = set(range(total))
    chosen: list[Chokepoint] = []

    while remaining:
        best = max(
            candidates,
            key=lambda cut: (len(cut.paths & remaining), cut.mine),
            default=None,
        )
        if best is None or not best.paths & remaining:
            break
        chosen.append(best)
        remaining -= best.paths

    return chosen


def _exact_cover(
    candidates: Sequence[Chokepoint], total: int, *, ceiling: int
) -> list[Chokepoint] | None:
    """The genuinely smallest cover, if one smaller than `ceiling` exists."""
    everything = frozenset(range(total))
    limit = min(ceiling - 1, _EXACT_DEPTH_LIMIT)

    for size in range(1, limit + 1):
        for combination in combinations(candidates, size):
            if _covers(combination, everything):
                return list(combination)

    return None


def _covers(cuts: Iterable[Chokepoint], everything: frozenset[int]) -> bool:
    reached: set[int] = set()
    for cut in cuts:
        reached |= cut.paths
    return reached >= everything


def _naive(radius: BlastRadius) -> int:
    """One change per exposed service per compromised package it pulls in."""
    return sum(len(service.targets) for service in radius.services)


@dataclass(slots=True)
class Verdict:
    """Whether a proposed bump actually exists and actually helps."""

    cut: Chokepoint
    to_version: str = ""
    checked: bool = False
    reachable: bool = False

    @property
    def available(self) -> bool:
        return bool(self.to_version)

    @property
    def clean(self) -> bool:
        return self.available and self.checked and not self.reachable


@dataclass(slots=True)
class Plan:
    remediation: Remediation
    verdicts: list[Verdict] = field(default_factory=list)
    rounds: int = 1

    @property
    def actionable(self) -> tuple[Verdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.clean)

    @property
    def blocked(self) -> tuple[Verdict, ...]:
        return tuple(verdict for verdict in self.verdicts if not verdict.clean)

    @property
    def followable(self) -> bool:
        """Whether following this plan actually ends the exposure."""
        return self.remediation.complete and not self.blocked


async def plan(
    client: HydraClient,
    radius: BlastRadius,
    *,
    attempts: int = 3,
    consistency: Consistency | None = None,
) -> Plan:
    """Propose changes, check each one is real, and route around the ones that are not.

    A hop with nowhere to upgrade to is a dead end, not a plan. When one turns up,
    it is banned and the cover is recomputed -- which usually moves the advice one
    step down the chain, from "bump this wrapper" to "bump what the wrapper pulls
    in".
    """
    banned: list[Link] = []
    attempt = 0

    while True:
        attempt += 1
        proposal = remediate(radius, avoid=banned)
        checked = await verify(client, proposal, radius.compromise, consistency=consistency)
        checked.rounds = attempt

        if checked.followable or attempt >= attempts or not checked.blocked:
            return checked

        banned.extend(verdict.cut.link for verdict in checked.blocked)


async def verify(
    client: HydraClient,
    remediation: Remediation,
    compromise: Compromise,
    *,
    candidates_per_cut: int = 6,
    consistency: Consistency | None = None,
) -> Plan:
    """Find a version to bump each cut to, and prove it does not lead back.

    "Upgrade and hope" is how a remediation quietly fails: the next version of a
    wrapper often depends on the same bad release. Every candidate upgrade for
    every cut is checked against every compromised release in a single traversal,
    and a candidate is only offered if the graph says it reaches none of them.
    """
    plan = Plan(remediation=remediation)
    if not remediation.cuts:
        return plan

    options: dict[Chokepoint, list[str]] = {}
    looked_up = await asyncio.gather(
        *(
            _upgrades(
                client,
                cut.link,
                compromise,
                limit=candidates_per_cut,
                consistency=consistency,
            )
            for cut in remediation.cuts
        )
    )
    for cut, found in zip(remediation.cuts, looked_up, strict=True):
        options[cut] = found

    # A pin of the compromised package itself is clean the moment the target
    # version is outside the compromise set -- there is no further edge to walk.
    # Checking those with MSpaths only burns the admission budget on a question
    # the answer already settled.
    needs_check = [
        key
        for cut, found in options.items()
        for key in found
        if not (cut.mine and cut.link.package in compromise.packages)
    ]
    tainted = await _reaching(client, needs_check, compromise, consistency=consistency)

    for cut in remediation.cuts:
        verdict = Verdict(cut=cut, checked=True)
        for key in options[cut]:
            pin_of_bad = cut.mine and cut.link.package in compromise.packages
            if pin_of_bad or key not in tainted:
                verdict.to_version = key.rpartition("@")[2]
                break
        else:
            verdict.reachable = bool(options[cut])
        plan.verdicts.append(verdict)

    return plan


async def _upgrades(
    client: HydraClient,
    link: Link,
    compromise: Compromise,
    *,
    limit: int,
    consistency: Consistency | None,
) -> list[str]:
    """Releases of this dependency newer than the one installed, nearest first.

    Nearest first because the smallest upgrade that works is the one most likely
    to be accepted.
    """
    if not link.package or not link.version:
        return []

    result = await client.run(
        "MATCH (r:Release) WHERE r.package = $package RETURN r.version AS version, r.key AS key",
        {"package": link.package},
        consistency=consistency,
    )

    newer: list[tuple[str, str]] = []
    for row in result.rows:
        version = str(row.get("version") or "")
        key = str(row.get("key") or "")
        if not version or not key or key in compromise.keys:
            continue
        if _newer(version, link.version):
            newer.append((version, key))

    newer.sort(key=cmp_to_key(lambda left, right: _compare(left[0], right[0])))
    return [key for _version, key in newer[:limit]]


async def _reaching(
    client: HydraClient,
    keys: Sequence[str],
    compromise: Compromise,
    *,
    consistency: Consistency | None,
) -> set[str]:
    """Which of these releases still reach a compromised one."""
    if not keys or not compromise.keys:
        return set()

    # Four hops is enough to catch "the next wrapper still pulls the bad
    # release". Deeper checks are what made `fix` hang on hub packages: the
    # frontier for a popular name at six hops trips admission control and then
    # negotiates down one hop at a time.
    query = paths.PathQuery(
        source=paths.Endpoint(str(Label.RELEASE), "key", tuple(keys)),
        target=paths.Endpoint(str(Label.RELEASE), "key", compromise.keys),
        rel_types=(str(Rel.DEPENDS_ON),),
        direction="outgoing",
        max_len=4,
        path_count=800,
    )
    found = await paths.run(client, query, consistency=consistency)

    return {
        str(path.source.get("key"))
        for path in found
        if path.source is not None and path.source.get("key")
    }


def _newer(candidate: str, installed: str) -> bool:
    try:
        return bool(semver.gt(candidate, installed, loose=True))
    except Exception:
        return False


def _compare(left: str, right: str) -> int:
    try:
        return int(semver.compare(left, right, loose=True))
    except Exception:
        # Unparseable versions sort last rather than breaking the ordering.
        return 1
