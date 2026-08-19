"""Capture live HydraDB responses into firestop/web/demo/*.json.

Run from the repo root after `docker compose up -d` and a populated graph:

    python scripts/export_demo.py

Overwrites the snapshot the hosted demo serves. No recrawl.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from firestop.api.schemas import BlastOut, PivotOut, PlanOut
from firestop.config import get_settings
from firestop.hydra.client import HydraClient
from firestop.query import blast as blast_query
from firestop.query import chokepoint, compromise, pivot
from firestop.schema import bootstrap

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "firestop" / "web" / "demo"

PACKAGE = "lodash"
VERSIONS = "<4.17.21"

_SERVICE_LIST = (
    "MATCH (s:Service) RETURN s.name AS name, s.criticality AS criticality, s.repo AS repo"
)
_TOP_PACKAGES = (
    "MATCH (p:Package) RETURN p.name AS name, p.dependent_count AS dependents "
    "ORDER BY dependents DESC LIMIT $limit"
)
_ADVISORY_LIST = (
    "MATCH (a:Advisory) RETURN a.osv_id AS osv_id, a.summary AS summary, "
    "a.severity AS severity, a.published_at AS published_at "
    "ORDER BY a.published_at DESC LIMIT $limit"
)


def _write(name: str, payload: object) -> None:
    DEMO.mkdir(parents=True, exist_ok=True)
    path = DEMO / name
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


async def main() -> None:
    async with HydraClient(get_settings()) as client:
        report = await bootstrap.check(client, include_census=True)
        _write(
            "health.json",
            {
                "ready": report.healthy,
                "read_epoch": report.read_epoch,
                "vertices": report.census.vertices,
                "errors": report.errors,
            },
        )

        services = await client.run(_SERVICE_LIST)
        packages = await client.run(_TOP_PACKAGES, {"limit": 12})
        advisories = await client.run(_ADVISORY_LIST, {"limit": 200})
        _write(
            "overview.json",
            {
                "services": [dict(row) for row in services.rows],
                "packages": [dict(row) for row in packages.rows],
                "advisories": [dict(row) for row in advisories.rows],
            },
        )

        found = await compromise.from_range(client, PACKAGE, VERSIONS)
        radius = await blast_query.exposure(client, found, include_build=True)
        _write("blast.json", BlastOut.of(radius, path_limit=25).model_dump(mode="json"))

        plan = await chokepoint.plan(client, radius)
        _write("fix.json", PlanOut.of(plan).model_dump(mode="json"))

        found_pivot = await pivot.pivot(client, PACKAGE, limit=8)
        _write("pivot.json", PivotOut.of(found_pivot).model_dump(mode="json"))


if __name__ == "__main__":
    asyncio.run(main())
