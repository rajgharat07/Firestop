"""Firestop command line."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import orjson
import typer
from rich.console import Console
from rich.table import Table

from firestop.config import get_settings
from firestop.eval import harness
from firestop.eval.harness import Report
from firestop.hydra.bolt import BoltClient
from firestop.hydra.client import HydraClient
from firestop.lockfile import org
from firestop.lockfile.ingest import LockfileIngest, LockfileStats
from firestop.npm.crawl import DEFAULT_DEV_HORIZON, Crawler, CrawlStats
from firestop.npm.registry import RegistryClient
from firestop.npm.resolve import Resolver
from firestop.osv.bulk import BulkExport
from firestop.osv.ingest import AdvisoryIngest, AdvisoryStats
from firestop.query import blast as blast_query
from firestop.query import chokepoint, compromise
from firestop.query import pivot as pivot_query
from firestop.query import typosquat as typosquat_query
from firestop.query.blast import BlastRadius
from firestop.query.compromise import Compromise, UnknownCompromise
from firestop.schema import bootstrap
from firestop.times import parse_timestamp

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Blast radius and chokepoint analysis for supply chain compromises.",
)
console = Console()


@app.callback()
def main() -> None:
    """Firestop."""


@app.command()
def doctor(
    check_bolt: bool = typer.Option(
        True, "--bolt/--no-bolt", help="Also verify the Bolt transport."
    ),
    show_census: bool = typer.Option(
        True, "--census/--no-census", help="Count what is currently in the graph."
    ),
    count_edges: bool = typer.Option(
        False, "--edges", help="Also count relationships. Slow on a populated graph."
    ),
) -> None:
    """Verify HydraDB is reachable, writable and readable."""
    exit_code = asyncio.run(
        _doctor(check_bolt=check_bolt, show_census=show_census, count_edges=count_edges)
    )
    raise typer.Exit(exit_code)


async def _doctor(*, check_bolt: bool, show_census: bool, count_edges: bool) -> int:
    settings = get_settings()

    async with HydraClient(settings) as client:
        report = await bootstrap.check(
            client, include_census=show_census, count_relationships=count_edges
        )

        if check_bolt and report.node_ready:
            async with BoltClient(settings) as bolt:
                report.bolt_ok = await bolt.verify()

    _render(report, show_census=show_census)
    return 0 if report.healthy else 1


def _render(report: bootstrap.HealthReport, *, show_census: bool) -> None:
    settings = get_settings()

    checks = Table(title="HydraDB", title_justify="left", show_edge=False)
    checks.add_column("check")
    checks.add_column("result")
    checks.add_column("detail", style="dim")

    checks.add_row(*_row("graph-node ready", report.node_ready, settings.hydradb_admin_url))
    checks.add_row(
        *_row("graph-indexer ready", report.indexer_ready, settings.hydradb_indexer_admin_url)
    )
    checks.add_row(*_row("write commits", report.write_ok, "MERGE over HTTP"))
    checks.add_row(
        *_row("read observes write", report.read_ok, f"strong read, epoch {report.read_epoch}")
    )
    if report.bolt_ok is not None:
        checks.add_row(*_row("bolt reachable", report.bolt_ok, settings.hydradb_bolt_url))

    console.print(checks)

    if show_census and report.census.is_empty:
        console.print()
        console.print("[dim]Graph is empty. Run `firestop crawl` to populate it.[/dim]")
    elif show_census:
        console.print()
        console.print(_census_table(report.census))

    for error in report.errors:
        console.print(f"[yellow]![/yellow] {error}")

    console.print()
    if report.healthy:
        console.print("[green]HydraDB is ready.[/green]")
    else:
        console.print("[red]HydraDB is not ready.[/red] See `docker compose ps` and logs.")


@app.command()
def crawl(
    packages: int = typer.Option(
        0, "--packages", "-n", help="Package ceiling. Defaults to CRAWL_MAX_PACKAGES."
    ),
    depth: int = typer.Option(4, "--depth", "-d", help="How far to walk from the seeds."),
    seed: list[str] = typer.Option(
        None, "--seed", "-s", help="Override the seed list. Repeatable."
    ),
    fresh: bool = typer.Option(
        False, "--fresh", help="Ignore any checkpoint and start the walk over."
    ),
    data_dir: Path = typer.Option(
        Path("data"), "--data-dir", help="Where the registry cache and checkpoint live."
    ),
    versions: int = typer.Option(
        40, "--versions", help="Most recent releases to keep per package."
    ),
    windows: int = typer.Option(
        5, "--windows", help="Most recent resolution windows to keep per dependency."
    ),
    dev_horizon: int = typer.Option(
        DEFAULT_DEV_HORIZON,
        "--dev-horizon",
        help="Hops from a seed within which dev dependencies are recorded.",
    ),
) -> None:
    """Crawl npm into the graph, with temporal dependency edges."""
    settings = get_settings()
    limit = packages or settings.crawl_max_packages

    stats = asyncio.run(
        _crawl(
            limit=limit,
            depth=depth,
            seeds=list(seed) if seed else None,
            resume=not fresh,
            data_dir=data_dir,
            max_versions=versions,
            max_windows=windows,
            dev_horizon=dev_horizon,
        )
    )
    _render_crawl(stats)


async def _crawl(
    *,
    limit: int,
    depth: int,
    seeds: list[str] | None,
    resume: bool,
    data_dir: Path,
    max_versions: int,
    max_windows: int,
    dev_horizon: int,
) -> CrawlStats:
    settings = get_settings()
    started = time.perf_counter()

    async with (
        HydraClient(settings) as hydra,
        RegistryClient(
            settings, cache_dir=data_dir / "registry", max_versions=max_versions
        ) as registry,
    ):
        if not await hydra.node_ready():
            console.print("[red]HydraDB is not ready.[/red] Run `firestop doctor` first.")
            raise typer.Exit(1)

        crawler = Crawler(
            hydra,
            registry,
            state_path=data_dir / "crawl-state.json",
            max_packages=limit,
            max_depth=depth,
            dev_horizon=dev_horizon,
            resolver=Resolver(max_windows=max_windows),
            progress=lambda message: console.print(f"[dim]{message}[/dim]"),
        )
        stats = await crawler.run(seeds, resume=resume)

    console.print(f"[dim]finished in {time.perf_counter() - started:.1f}s[/dim]")
    return stats


def _render_crawl(stats: CrawlStats) -> None:
    table = Table(title="Crawl", title_justify="left", show_edge=False)
    table.add_column("written")
    table.add_column("count", justify="right")
    table.add_column("registry")
    table.add_column("count", justify="right")

    written = [
        ("packages", stats.packages),
        ("releases", stats.releases),
        ("maintainers", stats.maintainers),
        ("installable edges", stats.depends_on),
        ("build-time edges", stats.dev_depends_on),
    ]
    registry = [
        ("fetched", stats.fetched),
        ("from cache", stats.cache_hits),
        ("not found", stats.missing),
        ("failed", stats.failed),
        ("non-semver ranges", stats.unresolvable_ranges),
        ("unsatisfied ranges", stats.unsatisfied_ranges),
        ("targets outside cap", stats.unwritten_targets),
    ]

    _side_by_side(table, written, registry)
    console.print()
    console.print(table)


def _side_by_side(
    table: Table,
    left: list[tuple[str, int | None]],
    right: list[tuple[str, int | None]],
) -> None:
    """Two label/count lists poured into one four-column table."""
    for index in range(max(len(left), len(right))):
        name, count = left[index] if index < len(left) else ("", "")
        other, other_count = right[index] if index < len(right) else ("", "")
        table.add_row(name, _count(count), other, _count(other_count))


def _count(value: int | str | None) -> str:
    if value is None:
        # Counted, but the node ran out of time. Not the same as zero.
        return "[dim]?[/dim]"
    return f"{value:,}" if value != "" else ""


@app.command()
def advisories(
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-download the OSV export instead of using the cached copy."
    ),
    data_dir: Path = typer.Option(
        Path("data"), "--data-dir", help="Where the OSV archive is cached."
    ),
) -> None:
    """Load OSV advisories and attach them to the releases they affect."""
    stats = asyncio.run(_advisories(refresh=refresh, data_dir=data_dir))
    _render_advisories(stats)


async def _advisories(*, refresh: bool, data_dir: Path) -> AdvisoryStats:
    settings = get_settings()
    started = time.perf_counter()

    async with HydraClient(settings) as hydra:
        if not await hydra.node_ready():
            console.print("[red]HydraDB is not ready.[/red] Run `firestop doctor` first.")
            raise typer.Exit(1)

        ingest = AdvisoryIngest(
            hydra,
            BulkExport(settings, cache_path=data_dir / "osv" / "npm-all.zip"),
            progress=lambda message: console.print(f"[dim]{message}[/dim]"),
        )
        stats = await ingest.run(refresh=refresh)

    console.print(f"[dim]finished in {time.perf_counter() - started:.1f}s[/dim]")
    return stats


def _render_advisories(stats: AdvisoryStats) -> None:
    table = Table(title="Advisories", title_justify="left", show_edge=False)
    table.add_column("written")
    table.add_column("count", justify="right")
    table.add_column("source")
    table.add_column("count", justify="right")

    written = [
        ("advisories", stats.advisories),
        ("affected releases", stats.affects),
    ]
    source = [
        ("records read", stats.records),
        ("npm advisories", stats.parsed),
        ("outside this graph", stats.out_of_scope),
        ("releases indexed", stats.releases_indexed),
        ("packages indexed", stats.packages_indexed),
        ("unreadable records", stats.malformed),
    ]

    _side_by_side(table, written, source)
    console.print()
    console.print(table)


@app.command()
def lockfiles(
    org_path: Path = typer.Option(
        Path("fixtures/acme"), "--org", help="Directory holding an org.json manifest."
    ),
) -> None:
    """Load an organisation's services and the releases their lockfiles pin."""
    try:
        loaded = org.load(org_path)
    except org.InvalidOrg as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    stats = asyncio.run(_lockfiles(loaded))
    _render_lockfiles(loaded, stats)


async def _lockfiles(loaded: org.Org) -> LockfileStats:
    settings = get_settings()
    started = time.perf_counter()

    async with HydraClient(settings) as hydra:
        if not await hydra.node_ready():
            console.print("[red]HydraDB is not ready.[/red] Run `firestop doctor` first.")
            raise typer.Exit(1)

        ingest = LockfileIngest(
            hydra, progress=lambda message: console.print(f"[dim]{message}[/dim]")
        )
        stats = await ingest.run(loaded)

    console.print(f"[dim]finished in {time.perf_counter() - started:.1f}s[/dim]")
    return stats


def _render_lockfiles(loaded: org.Org, stats: LockfileStats) -> None:
    table = Table(title=f"Org: {loaded.name}", title_justify="left", show_edge=False)
    table.add_column("service")
    table.add_column("lockfile")
    table.add_column("pinned", justify="right")
    table.add_column("in graph", justify="right")

    for name, kind, pinned, matched in stats.per_service:
        table.add_row(name, kind, f"{pinned:,}", f"{matched:,}")

    table.add_section()
    table.add_row("total", "", "", f"{stats.pins:,}")

    console.print()
    console.print(table)

    if stats.unmatched:
        # Expected: the crawl covers a slice of npm, not all of it.
        sample = ", ".join(stats.unmatched_sample)
        console.print(
            f"[dim]{stats.unmatched:,} pinned releases are not in the graph, "
            f"among them {sample}[/dim]"
        )


@app.command()
def evaluate(
    incidents: int = typer.Option(20, "--incidents", "-n", help="How many advisories to run."),
    output: Path = typer.Option(None, "--json", help="Also write the raw numbers here."),
) -> None:
    """Check the graph-native answer against a hand-rolled traversal, and time both."""
    report = asyncio.run(_evaluate(incidents))
    _render_evaluation(report)

    if output:
        output.write_text(_evaluation_json(report), encoding="utf-8")
        console.print(f"[dim]wrote {output}[/dim]")


async def _evaluate(incidents: int) -> Report:
    async with HydraClient(get_settings()) as hydra:
        console.print(f"[dim]running {incidents} incidents through both approaches[/dim]")
        return await harness.evaluate(hydra, limit=incidents)


def _render_evaluation(report: Report) -> None:
    if not report.cases:
        console.print("[yellow]No advisory in the graph touches a crawled release.[/yellow]")
        return

    table = Table(title="Evaluation", title_justify="left", show_edge=False)
    table.add_column("advisory")
    table.add_column("exposed", justify="right")
    table.add_column("firestop", justify="right")
    table.add_column("by hand", justify="right")
    table.add_column("trips", justify="right")
    table.add_column("agrees")

    for case in report.cases:
        table.add_row(
            case.advisory,
            str(case.exposed),
            f"{case.firestop_ms:.0f} ms",
            f"{case.baseline_ms:.0f} ms",
            str(case.round_trips),
            "[green]yes[/green]" if case.agrees else "[red]no[/red]",
        )

    console.print()
    console.print(table)
    console.print()
    console.print(
        f"Agreement with the reference traversal: {report.agreement:.0%}. "
        f"Median speedup: {report.median_speedup:.1f}x over "
        f"{report.round_trips:,} round trips it did not have to make."
    )
    console.print(
        f"Name similarity on the same incidents: precision "
        f"{report.similarity_precision:.0%}, recall {report.similarity_recall:.0%}. "
        "Exposure is a property of the graph, not of the name."
    )


def _evaluation_json(report: Report) -> str:
    payload = {
        "agreement": report.agreement,
        "median_speedup": report.median_speedup,
        "round_trips_avoided": report.round_trips,
        "similarity_precision": report.similarity_precision,
        "similarity_recall": report.similarity_recall,
        "cases": [
            {
                "advisory": case.advisory,
                "severity": case.severity,
                "exposed": case.exposed,
                "firestop_ms": round(case.firestop_ms, 1),
                "baseline_ms": round(case.baseline_ms, 1),
                "round_trips": case.round_trips,
                "agrees": case.agrees,
                "similarity_precision": case.similarity.precision,
                "similarity_recall": case.similarity.recall,
            }
            for case in report.cases
        ],
    }
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    port: int = typer.Option(8000, "--port", help="Port to bind."),
    reload: bool = typer.Option(False, "--reload", help="Restart on code changes."),
) -> None:
    """Serve the read-only API and web console."""
    import uvicorn

    console.print(f"[dim]Firestop on http://{host}:{port}[/dim]")
    uvicorn.run("firestop.api.app:app", host=host, port=port, reload=reload, log_level="info")


@app.command()
def blast(
    advisory: str = typer.Option("", "--advisory", "-a", help="An OSV or GHSA identifier."),
    package: str = typer.Option("", "--package", "-p", help="A package to treat as compromised."),
    versions: str = typer.Option(
        "*", "--versions", "-v", help="Which versions of it, as a semver range."
    ),
    as_of: str = typer.Option(
        "", "--as-of", help="Answer as of this date (YYYY-MM-DD) rather than now."
    ),
    include_build: bool = typer.Option(
        True, "--build/--no-build", help="Count exposure that only reaches CI."
    ),
    show_paths: int = typer.Option(3, "--paths", help="How many paths to show per service."),
) -> None:
    """Which services are exposed to a compromised release, and how they reach it."""
    moment = _moment(as_of)
    radius = asyncio.run(
        _blast(
            advisory=advisory,
            package=package,
            versions=versions,
            as_of=moment,
            include_build=include_build,
        )
    )
    _render_blast(radius, show_paths=show_paths)


async def _blast(
    *, advisory: str, package: str, versions: str, as_of: int | None, include_build: bool
) -> BlastRadius:
    async with HydraClient(get_settings()) as hydra:
        found = await _resolve_compromise(hydra, advisory, package, versions)
        return await blast_query.exposure(hydra, found, as_of=as_of, include_build=include_build)


@app.command()
def fix(
    advisory: str = typer.Option("", "--advisory", "-a", help="An OSV or GHSA identifier."),
    package: str = typer.Option("", "--package", "-p", help="A package to treat as compromised."),
    versions: str = typer.Option(
        "*", "--versions", "-v", help="Which versions of it, as a semver range."
    ),
    as_of: str = typer.Option("", "--as-of", help="Plan as of this date rather than now."),
    include_build: bool = typer.Option(
        True, "--build/--no-build", help="Count exposure that only reaches CI."
    ),
) -> None:
    """The fewest changes that cut every path, each checked against the graph."""
    moment = _moment(as_of)
    radius, proposal = asyncio.run(
        _fix(
            advisory=advisory,
            package=package,
            versions=versions,
            as_of=moment,
            include_build=include_build,
        )
    )
    _render_fix(radius, proposal)


async def _fix(
    *, advisory: str, package: str, versions: str, as_of: int | None, include_build: bool
) -> tuple[BlastRadius, chokepoint.Plan]:
    async with HydraClient(get_settings()) as hydra:
        found = await _resolve_compromise(hydra, advisory, package, versions)
        radius = await blast_query.exposure(hydra, found, as_of=as_of, include_build=include_build)
        return radius, await chokepoint.plan(hydra, radius)


async def _resolve_compromise(
    hydra: HydraClient, advisory: str, package: str, versions: str
) -> Compromise:
    if not advisory and not package:
        console.print("[red]Name what was compromised:[/red] --advisory or --package.")
        raise typer.Exit(2)

    try:
        if advisory:
            return await compromise.from_advisory(hydra, advisory)
        return await compromise.from_range(hydra, package, versions)
    except UnknownCompromise as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@app.command()
def pivot(
    package: str = typer.Argument(..., help="The package whose publishers to expand."),
    limit: int = typer.Option(15, "--limit", help="How many sibling packages to show."),
) -> None:
    """Everything else the accounts behind a package are able to publish."""
    found = asyncio.run(_pivot(package, limit))
    _render_pivot(found)


async def _pivot(package: str, limit: int) -> pivot_query.Pivot:
    async with HydraClient(get_settings()) as hydra:
        return await pivot_query.pivot(hydra, package, limit=limit)


def _render_pivot(found: pivot_query.Pivot) -> None:
    console.print()
    if not found.maintainers:
        console.print(f"[yellow]No publisher recorded for {found.package}.[/yellow]")
        return

    console.print(
        f"[bold]{found.package}[/bold] can be published by "
        f"{', '.join(found.maintainers)} ({len(found.maintainers)} account(s))"
    )

    if not found.siblings:
        console.print("[green]Those accounts publish nothing else in this graph.[/green]")
        return

    table = Table(show_edge=False, title="Also publishable by the same accounts")
    table.add_column("package")
    table.add_column("dependents", justify="right")
    table.add_column("via")

    for sibling in found.siblings:
        table.add_row(sibling.package, f"{sibling.dependents:,}", ", ".join(sibling.maintainers))

    console.print(table)
    console.print()
    console.print(
        f"A compromise of any of these accounts puts {found.reach} more package(s) "
        f"and {found.dependents:,} dependent(s) in scope."
    )


@app.command()
def typosquat(
    popular_at: int = typer.Option(
        typosquat_query.POPULAR_AT, "--popular-at", help="Dependents that make a name a target."
    ),
    max_distance: int = typer.Option(1, "--distance", help="Edits allowed between the names."),
    limit: int = typer.Option(20, "--limit", help="How many suspects to show."),
) -> None:
    """Names one typo away from a popular package, filtered by who publishes them."""
    radar = asyncio.run(_typosquat(popular_at, max_distance, limit))
    _render_typosquat(radar)


async def _typosquat(popular_at: int, max_distance: int, limit: int) -> typosquat_query.Radar:
    async with HydraClient(get_settings()) as hydra:
        return await typosquat_query.scan(
            hydra, popular_at=popular_at, max_distance=max_distance, limit=limit
        )


def _render_typosquat(radar: typosquat_query.Radar) -> None:
    console.print()
    if not radar.suspects:
        console.print(
            f"[green]Nothing resembles the {radar.popular} popular name(s) closely enough.[/green]"
        )
        return

    table = Table(show_edge=False, title="Names worth a second look", title_justify="left")
    table.add_column("suspect")
    table.add_column("resembles")
    table.add_column("trick")
    table.add_column("dependents", justify="right")
    table.add_column("target has", justify="right")

    for suspect in radar.suspects:
        table.add_row(
            suspect.name,
            suspect.resembles,
            suspect.trick,
            f"{suspect.dependents:,}",
            f"{suspect.target_dependents:,}",
        )

    console.print(table)
    console.print()
    console.print(
        f"[dim]{radar.examined:,} names compared against {radar.popular:,} popular ones. "
        f"Candidates sharing a publisher with what they resemble were dropped.[/dim]"
    )


def _render_blast(radius: BlastRadius, *, show_paths: int) -> None:
    console.print()
    console.print(_headline(radius.compromise))

    if radius.clean:
        console.print("[green]No service reaches it.[/green]")
        _render_traversal_note(radius)
        return

    table = Table(show_edge=False, title="Exposed services", title_justify="left")
    table.add_column("service")
    table.add_column("tier")
    table.add_column("hops", justify="right")
    table.add_column("paths", justify="right")
    table.add_column("reaches")

    for found in radius.services:
        table.add_row(
            found.service,
            found.criticality,
            str(found.shortest),
            str(len(found.paths)),
            "runtime" if found.runtime else "[dim]build only[/dim]",
        )

    console.print(table)

    for found in radius.services[:show_paths]:
        console.print()
        console.print(f"[bold]{found.service}[/bold]")
        for path in found.paths[:show_paths]:
            console.print(f"  {' -> '.join(path.chain)}")

    _render_traversal_note(radius)


def _render_fix(radius: BlastRadius, proposal: chokepoint.Plan) -> None:
    remediation = proposal.remediation
    console.print()
    console.print(_headline(radius.compromise))

    if radius.clean:
        console.print("[green]No service reaches it. Nothing to change.[/green]")
        return

    table = Table(show_edge=False, title="Suggested changes", title_justify="left")
    table.add_column("change")
    table.add_column("to", justify="right")
    table.add_column("cuts", justify="right")
    table.add_column("owner")

    for verdict in proposal.verdicts:
        cut = verdict.cut
        table.add_row(
            cut.describe(),
            verdict.to_version or "[red]nothing clean[/red]",
            str(cut.severs),
            "ours" if cut.mine else "upstream",
        )

    console.print(table)
    console.print()
    console.print(
        f"{len(remediation.cuts)} change(s) cut {remediation.severed} of "
        f"{remediation.paths} live paths across {radius.exposed} service(s). "
        f"Bumping every service separately would take {remediation.naive}."
    )

    if proposal.blocked:
        console.print(
            "[yellow]Some hops have no clean version to move to.[/yellow] "
            "Those need an upstream release or a package override."
        )
    if not remediation.exact:
        console.print("[dim]Too many candidates to prove minimality; this is a good cover.[/dim]")


def _headline(found: Compromise) -> str:
    label = found.advisory or found.summary or ", ".join(found.packages)
    detail = f"{len(found.keys)} release(s) of {', '.join(found.packages)}"
    severity = f" [{found.severity}]" if found.severity else ""
    return f"[bold]{label}[/bold]{severity} - {detail}"


def _render_traversal_note(radius: BlastRadius) -> None:
    console.print()
    console.print(
        f"[dim]{radius.paths_returned:,} paths returned, {radius.paths_live:,} live "
        f"after interval filtering, in {radius.elapsed_ms:.0f} ms[/dim]"
    )
    if radius.truncated:
        console.print(
            "[yellow]The traversal hit its path ceiling.[/yellow] "
            "Raise --paths-considered for a complete answer."
        )
    if radius.shortened:
        console.print(
            f"[yellow]Answered within {radius.depth} hops rather than "
            f"{radius.asked_for}.[/yellow] The node refused the wider frontier, so a "
            "path longer than this would not have been seen."
        )


def _moment(as_of: str) -> int | None:
    if not as_of:
        return None
    parsed = parse_timestamp(as_of if "T" in as_of else f"{as_of}T00:00:00Z")
    if parsed is None:
        console.print(f"[red]Cannot read {as_of!r} as a date.[/red] Use YYYY-MM-DD.")
        raise typer.Exit(2)
    return parsed


def _census_table(census: bootstrap.Census) -> Table:
    table = Table(title="Graph", title_justify="left", show_edge=False)
    table.add_column("vertices")
    table.add_column("count", justify="right")
    table.add_column("relationships")
    table.add_column("count", justify="right")

    # Empty labels are noise; ones that timed out are not, since an unknown count
    # is exactly what a reader needs to see.
    vertices = [(k, v) for k, v in census.vertices.items() if v or v is None]
    relationships = [(k, v) for k, v in census.relationships.items() if v or v is None]

    _side_by_side(table, vertices, relationships)

    if not relationships:
        table.caption = "relationships not counted; pass --edges"
        table.caption_justify = "left"

    table.add_section()
    table.add_row(
        "total",
        f"{census.total_vertices:,}",
        "total",
        f"{census.total_relationships:,}" if relationships else "",
    )
    return table


def _row(name: str, ok: bool, detail: str) -> tuple[str, str, str]:
    return name, "[green]pass[/green]" if ok else "[red]fail[/red]", detail


if __name__ == "__main__":
    app()
