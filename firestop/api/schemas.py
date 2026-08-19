"""Wire-facing response models (separate from query dataclasses)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from firestop.query.blast import BlastRadius, ExposurePath, Reach, ServiceExposure
from firestop.query.chokepoint import Plan, Verdict
from firestop.query.compromise import Compromise
from firestop.query.pivot import Pivot
from firestop.query.typosquat import Radar
from firestop.schema.model import OPEN_INTERVAL_END


class CompromiseOut(BaseModel):
    advisory: str = ""
    summary: str = ""
    severity: str = ""
    packages: list[str] = Field(default_factory=list)
    releases: list[str] = Field(default_factory=list)
    fixed_in: list[str] = Field(default_factory=list)
    introduced_at: int = 0

    @classmethod
    def of(cls, found: Compromise) -> CompromiseOut:
        return cls(
            advisory=found.advisory,
            summary=found.summary,
            severity=found.severity,
            packages=list(found.packages),
            releases=list(found.keys),
            fixed_in=list(found.fixed_in),
            introduced_at=found.introduced_at,
        )


class PathOut(BaseModel):
    target: str
    entry: str
    hops: int
    chain: list[str]
    build_time: bool
    direct: bool
    valid_from: int
    valid_to: int | None

    @classmethod
    def of(cls, path: ExposurePath) -> PathOut:
        return cls(
            target=path.target,
            entry=path.entry,
            hops=path.hops,
            chain=list(path.chain),
            build_time=path.build_time,
            direct=path.direct,
            valid_from=path.window.valid_from,
            # An open interval is "still true", which reads better as null than as
            # a sentinel year the client would have to know about.
            valid_to=None if path.window.valid_to >= OPEN_INTERVAL_END else path.window.valid_to,
        )


class ServiceOut(BaseModel):
    service: str
    criticality: str
    shortest: int
    runtime: bool
    direct: bool
    targets: list[str]
    paths: list[PathOut]

    @classmethod
    def of(cls, found: ServiceExposure, *, path_limit: int) -> ServiceOut:
        return cls(
            service=found.service,
            criticality=found.criticality,
            shortest=found.shortest,
            runtime=found.runtime,
            direct=found.direct,
            targets=list(found.targets),
            paths=[PathOut.of(path) for path in found.paths[:path_limit]],
        )


class BlastOut(BaseModel):
    compromise: CompromiseOut
    as_of: int | None
    services: list[ServiceOut]
    exposed: int
    paths_returned: int
    paths_live: int
    truncated: bool
    depth: int
    shortened: bool
    elapsed_ms: float

    @classmethod
    def of(cls, radius: BlastRadius, *, path_limit: int = 25) -> BlastOut:
        return cls(
            compromise=CompromiseOut.of(radius.compromise),
            as_of=radius.as_of,
            services=[ServiceOut.of(found, path_limit=path_limit) for found in radius.services],
            exposed=radius.exposed,
            paths_returned=radius.paths_returned,
            paths_live=radius.paths_live,
            truncated=radius.truncated,
            depth=radius.depth,
            shortened=radius.shortened,
            elapsed_ms=round(radius.elapsed_ms, 1),
        )


class ChangeOut(BaseModel):
    description: str
    holder: str
    package: str
    from_version: str
    to_version: str
    severs: int
    mine: bool
    blocked: bool

    @classmethod
    def of(cls, verdict: Verdict) -> ChangeOut:
        link = verdict.cut.link
        return cls(
            description=verdict.cut.describe(),
            holder=link.holder,
            package=link.package,
            from_version=link.version,
            to_version=verdict.to_version,
            severs=verdict.cut.severs,
            mine=verdict.cut.mine,
            blocked=not verdict.clean,
        )


class PlanOut(BaseModel):
    changes: list[ChangeOut]
    paths: int
    severed: int
    naive: int
    saved: int
    complete: bool
    followable: bool
    exact: bool
    rounds: int

    @classmethod
    def of(cls, plan: Plan) -> PlanOut:
        remediation = plan.remediation
        return cls(
            changes=[ChangeOut.of(verdict) for verdict in plan.verdicts],
            paths=remediation.paths,
            severed=remediation.severed,
            naive=remediation.naive,
            saved=remediation.saved,
            complete=remediation.complete,
            followable=plan.followable,
            exact=remediation.exact,
            rounds=plan.rounds,
        )


class ReachOut(BaseModel):
    packages: list[str]
    releases: int
    max_depth: int
    truncated: bool
    elapsed_ms: float

    @classmethod
    def of(cls, spread: Reach) -> ReachOut:
        return cls(
            packages=list(spread.packages),
            releases=spread.releases,
            max_depth=spread.max_depth,
            truncated=spread.truncated,
            elapsed_ms=round(spread.elapsed_ms, 1),
        )


class PivotOut(BaseModel):
    package: str
    maintainers: list[str]
    siblings: list[dict]
    reach: int
    dependents: int

    @classmethod
    def of(cls, found: Pivot) -> PivotOut:
        return cls(
            package=found.package,
            maintainers=list(found.maintainers),
            siblings=[
                {
                    "package": sibling.package,
                    "dependents": sibling.dependents,
                    "maintainers": list(sibling.maintainers),
                }
                for sibling in found.siblings
            ],
            reach=found.reach,
            dependents=found.dependents,
        )


class TyposquatOut(BaseModel):
    popular: int
    examined: int
    suspects: list[dict]

    @classmethod
    def of(cls, radar: Radar) -> TyposquatOut:
        return cls(
            popular=radar.popular,
            examined=radar.examined,
            suspects=[
                {
                    "name": suspect.name,
                    "resembles": suspect.resembles,
                    "trick": suspect.trick,
                    "dependents": suspect.dependents,
                    "target_dependents": suspect.target_dependents,
                    "shares_maintainer": suspect.shares_maintainer,
                }
                for suspect in radar.suspects
            ],
        )
