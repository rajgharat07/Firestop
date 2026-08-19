"""Read-only HTTP API. Ingest stays in the CLI; one Hydra client per process."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from firestop.api.schemas import (
    BlastOut,
    PivotOut,
    PlanOut,
    ReachOut,
    TyposquatOut,
)
from firestop.config import get_settings
from firestop.hydra.client import HydraClient
from firestop.query import blast as blast_query
from firestop.query import chokepoint, compromise, pivot, typosquat
from firestop.query.compromise import Compromise, UnknownCompromise
from firestop.schema import bootstrap

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

_ADVISORY_LIST = (
    "MATCH (a:Advisory) RETURN a.osv_id AS osv_id, a.summary AS summary, "
    "a.severity AS severity, a.published_at AS published_at "
    "ORDER BY a.published_at DESC LIMIT $limit"
)

_SERVICE_LIST = (
    "MATCH (s:Service) RETURN s.name AS name, s.criticality AS criticality, s.repo AS repo"
)

_TOP_PACKAGES = (
    "MATCH (p:Package) RETURN p.name AS name, p.dependent_count AS dependents "
    "ORDER BY dependents DESC LIMIT $limit"
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.hydra = HydraClient(get_settings())
    try:
        yield
    finally:
        await app.state.hydra.close()


app = FastAPI(
    title="Firestop",
    description="Blast radius and chokepoint analysis for supply chain compromises.",
    version="0.1.0",
    lifespan=lifespan,
)


def hydra() -> HydraClient:
    return app.state.hydra


async def _compromise(
    client: HydraClient, advisory: str, package: str, versions: str
) -> Compromise:
    if not advisory and not package:
        raise HTTPException(400, "name what was compromised: advisory or package")
    try:
        if advisory:
            return await compromise.from_advisory(client, advisory)
        return await compromise.from_range(client, package, versions or "*")
    except UnknownCompromise as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/health")
async def health(client: HydraClient = Depends(hydra)) -> dict:
    report = await bootstrap.check(client, include_census=True)
    return {
        "ready": report.healthy,
        "read_epoch": report.read_epoch,
        "vertices": report.census.vertices,
        "errors": report.errors,
    }


@app.get("/api/overview")
async def overview(client: HydraClient = Depends(hydra)) -> dict:
    services = await client.run(_SERVICE_LIST)
    packages = await client.run(_TOP_PACKAGES, {"limit": 12})
    advisories = await client.run(_ADVISORY_LIST, {"limit": 200})

    return {
        "services": [dict(row) for row in services.rows],
        "packages": [dict(row) for row in packages.rows],
        "advisories": [dict(row) for row in advisories.rows],
    }


@app.get("/api/blast", response_model=BlastOut)
async def blast(
    advisory: str = "",
    package: str = "",
    versions: str = "*",
    as_of: int | None = None,
    build: bool = True,
    paths: int = Query(default=25, ge=1, le=200),
    client: HydraClient = Depends(hydra),
) -> BlastOut:
    found = await _compromise(client, advisory, package, versions)
    radius = await blast_query.exposure(client, found, as_of=as_of, include_build=build)
    return BlastOut.of(radius, path_limit=paths)


@app.get("/api/fix", response_model=PlanOut)
async def fix(
    advisory: str = "",
    package: str = "",
    versions: str = "*",
    as_of: int | None = None,
    build: bool = True,
    client: HydraClient = Depends(hydra),
) -> PlanOut:
    found = await _compromise(client, advisory, package, versions)
    radius = await blast_query.exposure(client, found, as_of=as_of, include_build=build)
    return PlanOut.of(await chokepoint.plan(client, radius))


@app.get("/api/reach", response_model=ReachOut)
async def reach(
    advisory: str = "",
    package: str = "",
    versions: str = "*",
    as_of: int | None = None,
    client: HydraClient = Depends(hydra),
) -> ReachOut:
    found = await _compromise(client, advisory, package, versions)
    return ReachOut.of(await blast_query.reach(client, found, as_of=as_of))


@app.get("/api/pivot", response_model=PivotOut)
async def maintainer_pivot(
    package: str,
    limit: int = Query(default=50, ge=1, le=500),
    client: HydraClient = Depends(hydra),
) -> PivotOut:
    return PivotOut.of(await pivot.pivot(client, package, limit=limit))


@app.get("/api/typosquat", response_model=TyposquatOut)
async def typosquat_radar(
    popular_at: int = Query(default=typosquat.POPULAR_AT, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    client: HydraClient = Depends(hydra),
) -> TyposquatOut:
    return TyposquatOut.of(await typosquat.scan(client, popular_at=popular_at, limit=limit))


def _page(name: str) -> FileResponse:
    return FileResponse(WEB_ROOT / name)


@app.get("/")
async def home() -> FileResponse:
    return _page("home.html")


@app.get("/console")
async def console() -> FileResponse:
    return _page("console.html")


@app.get("/how-it-works")
async def how_it_works() -> FileResponse:
    return _page("how-it-works.html")


@app.get("/features")
async def features() -> FileResponse:
    return _page("features.html")


@app.get("/architecture")
async def architecture() -> FileResponse:
    return _page("architecture.html")


if WEB_ROOT.exists():
    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")
