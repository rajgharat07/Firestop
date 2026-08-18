from __future__ import annotations

import httpx
import respx

from firestop.hydra.client import HydraClient
from firestop.query.blast import BlastRadius, ExposurePath, Link, ServiceExposure
from firestop.query.chokepoint import plan, remediate, verify
from firestop.query.compromise import Compromise
from firestop.query.temporal import Window
from tests.conftest import QUERY_URL, page, value
from tests.paths import release_path

COMPROMISE = Compromise(packages=("evil",), keys=("evil@1.4.2",), advisory="GHSA-test")


def link(holder: str, depends_on: str, *, pin: bool = False) -> Link:
    package, _, version = depends_on.rpartition("@")
    return Link(
        relationship="PINS" if pin else "DEPENDS_ON",
        holder=holder,
        depends_on=depends_on,
        package=package,
        version=version,
    )


def path(service: str, *chain: Link) -> ExposurePath:
    return ExposurePath(
        service=service,
        target=chain[-1].depends_on,
        entry=chain[0].depends_on,
        hops=len(chain),
        chain=(service, *(step.depends_on for step in chain)),
        links=chain,
        window=Window(),
        build_time=False,
        direct=False,
    )


def radius(*services: tuple[str, list[ExposurePath]]) -> BlastRadius:
    return BlastRadius(
        compromise=COMPROMISE,
        as_of=None,
        services=[
            ServiceExposure(service=name, criticality="tier-1", paths=tuple(found))
            for name, found in services
        ],
    )


class TestFindingTheFewestChanges:
    def test_one_shared_hop_cuts_every_path_through_it(self):
        # Shared hop → one bump covers all three paths.
        shared = link("express@4.17.1", "evil@1.4.2")
        found = radius(
            *(
                (
                    name,
                    [
                        path(
                            name,
                            link(name, "express@4.17.1", pin=True),
                            shared,
                        )
                    ],
                )
                for name in ("checkout-api", "web-storefront", "batch-jobs")
            )
        )

        plan = remediate(found)

        assert len(plan.cuts) == 1
        assert plan.cuts[0].link.depends_on == "evil@1.4.2"
        assert plan.complete

    def test_the_widest_cut_is_preferred_over_several_narrow_ones(self):
        wide = link("express@4.17.1", "evil@1.4.2")
        found = radius(
            (
                "checkout-api",
                [
                    path("checkout-api", link("checkout-api", "express@4.17.1", pin=True), wide),
                    path(
                        "checkout-api",
                        link("checkout-api", "koa@2.13.4", pin=True),
                        link("koa@2.13.4", "express@4.17.1"),
                        wide,
                    ),
                ],
            )
        )

        plan = remediate(found)

        assert [cut.link.depends_on for cut in plan.cuts] == ["evil@1.4.2"]
        assert plan.cuts[0].severs == 2

    def test_separate_routes_each_need_their_own_change(self):
        found = radius(
            (
                "checkout-api",
                [
                    path(
                        "checkout-api",
                        link("checkout-api", "express@4.17.1", pin=True),
                        link("express@4.17.1", "evil@1.4.2"),
                    ),
                    path(
                        "checkout-api",
                        link("checkout-api", "pg@8.7.1", pin=True),
                        link("pg@8.7.1", "evil@1.4.3"),
                    ),
                ],
            )
        )

        plan = remediate(found)

        assert len(plan.cuts) == 2
        assert plan.complete

    def test_the_exact_search_beats_greedy_when_greedy_is_wrong(self):
        # Greedy takes the widest cut first (three paths), then still needs two
        # more. Two narrower cuts cover everything.
        wide = link("shared@1.0.0", "evil@1.4.2")
        left = link("left@1.0.0", "evil@1.4.2")
        right = link("right@1.0.0", "evil@1.4.2")

        paths = [
            path("a", link("a", "shared@1.0.0", pin=True), wide, left),
            path("b", link("b", "shared@1.0.0", pin=True), wide, left),
            path("c", link("c", "shared@1.0.0", pin=True), wide, right),
            path("d", link("d", "left@1.0.0", pin=True), left),
            path("e", link("e", "right@1.0.0", pin=True), right),
        ]
        plan = remediate(radius(("a", paths)))

        assert len(plan.cuts) == 2
        assert {cut.link.holder for cut in plan.cuts} == {"left@1.0.0", "right@1.0.0"}
        assert plan.exact

    def test_a_service_link_to_its_own_lockfile_is_not_a_remedy(self):
        # Cutting it means deleting the service.
        found = radius(
            (
                "checkout-api",
                [
                    path(
                        "checkout-api",
                        Link(
                            relationship="USES_LOCKFILE",
                            holder="checkout-api",
                            depends_on="checkout-api/package-lock.json",
                            package="",
                            version="",
                        ),
                        link("checkout-api", "evil@1.4.2", pin=True),
                    )
                ],
            )
        )

        plan = remediate(found)

        assert all(cut.link.relationship != "USES_LOCKFILE" for cut in plan.cuts)

    def test_changes_we_own_are_separated_from_ones_we_have_to_ask_for(self):
        # Two services share express, so cutting there beats bumping both pins. A
        # third pins the bad release itself, which is ours to fix.
        shared = link("express@4.17.1", "evil@1.4.2")
        found = radius(
            (
                "checkout-api",
                [
                    path("checkout-api", link("checkout-api", "express@4.17.1", pin=True), shared),
                    path("batch-jobs", link("batch-jobs", "express@4.17.1", pin=True), shared),
                    path("admin-console", link("admin-console", "evil@1.4.2", pin=True)),
                ],
            )
        )

        plan = remediate(found)

        assert len(plan.mine) == 1
        assert len(plan.upstream) == 1
        assert plan.complete

    def test_a_change_in_our_own_repository_wins_a_tie(self):
        # Both cut one path. The one that does not need another maintainer's
        # release is the one to suggest.
        found = radius(
            (
                "checkout-api",
                [path("checkout-api", link("checkout-api", "evil@1.4.2", pin=True))],
            )
        )

        plan = remediate(found)

        assert plan.cuts[0].mine

    def test_the_saving_against_the_obvious_approach_is_reported(self):
        shared = link("express@4.17.1", "evil@1.4.2")
        found = radius(
            *(
                (name, [path(name, link(name, "express@4.17.1", pin=True), shared)])
                for name in ("a", "b", "c", "d")
            )
        )

        plan = remediate(found)

        assert plan.naive == 4
        assert len(plan.cuts) == 1
        assert plan.saved == 3

    def test_a_clean_blast_radius_needs_no_changes(self):
        plan = remediate(radius())

        assert plan.cuts == ()
        assert not plan.complete


class TestCheckingTheUpgradeIsReal:
    def plan_for(self, holder: str, depends_on: str):
        return remediate(
            radius(
                (
                    "checkout-api",
                    [path("checkout-api", link(holder, depends_on, pin=True))],
                )
            )
        )

    @respx.mock
    async def test_an_upgrade_that_escapes_the_compromise_is_offered(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=page(
                        ["version", "key"],
                        [
                            [value("string", "1.4.3"), value("string", "evil@1.4.3")],
                            [value("string", "1.4.2"), value("string", "evil@1.4.2")],
                        ],
                    ),
                ),
                httpx.Response(200, json=page(["path"], [])),
            ]
        )

        checked = await verify(client, self.plan_for("checkout-api", "evil@1.4.2"), COMPROMISE)

        assert checked.verdicts[0].to_version == "1.4.3"
        assert checked.actionable

    @respx.mock
    async def test_an_upgrade_that_still_leads_back_is_refused(self, client: HydraClient):
        # Upgrade still reaches the bad release → refuse.
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=page(
                        ["version", "key"],
                        [[value("string", "4.18.0"), value("string", "express@4.18.0")]],
                    ),
                ),
                httpx.Response(
                    200,
                    json=page(
                        ["path"],
                        [[release_path(("express", "4.18.0"), ("evil", "1.4.2"))]],
                    ),
                ),
            ]
        )

        checked = await verify(client, self.plan_for("checkout-api", "express@4.17.1"), COMPROMISE)

        assert checked.verdicts[0].to_version == ""
        assert checked.blocked

    @respx.mock
    async def test_a_dependency_with_nothing_newer_is_blocked(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=page(
                        ["version", "key"],
                        [[value("string", "1.0.0"), value("string", "evil@1.0.0")]],
                    ),
                ),
                httpx.Response(200, json=page(["path"], [])),
            ]
        )

        checked = await verify(client, self.plan_for("checkout-api", "evil@1.4.2"), COMPROMISE)

        assert not checked.actionable
        assert checked.blocked

    @respx.mock
    async def test_a_dead_end_makes_the_planner_try_somewhere_else(self, client: HydraClient):
        # Bumping the wrapper leads nowhere, so the advice should move down the
        # chain rather than repeat itself.
        shared = link("express@4.17.1", "evil@1.4.2")
        found = radius(
            (
                "checkout-api",
                [
                    path("checkout-api", link("checkout-api", "express@4.17.1", pin=True), shared),
                    path("batch-jobs", link("batch-jobs", "express@4.17.1", pin=True), shared),
                ],
            )
        )
        no_escape = page(
            ["version", "key"],
            [[value("string", "4.18.0"), value("string", "express@4.18.0")]],
        )
        escapes = page(
            ["version", "key"],
            [[value("string", "1.4.4"), value("string", "evil@1.4.4")]],
        )
        respx.post(QUERY_URL).mock(
            side_effect=[
                # First round: the only cut is express -> evil, and every newer
                # express still reaches it.
                httpx.Response(200, json=no_escape),
                httpx.Response(
                    200,
                    json=page(
                        ["path"],
                        [[release_path(("express", "4.18.0"), ("evil", "1.4.2"))]],
                    ),
                ),
                # Second round: the pins, which can move to a clean release.
                httpx.Response(200, json=no_escape),
                httpx.Response(200, json=escapes),
                httpx.Response(200, json=page(["path"], [])),
            ]
        )

        checked = await plan(client, found, attempts=2)

        assert checked.rounds == 2
        assert all(verdict.cut.link.depends_on != "evil@1.4.2" for verdict in checked.verdicts)

    @respx.mock
    async def test_a_compromised_version_is_never_offered_as_the_fix(self, client: HydraClient):
        compromise = Compromise(
            packages=("evil",), keys=("evil@1.4.2", "evil@1.4.3"), advisory="GHSA-test"
        )
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=page(
                        ["version", "key"],
                        [
                            [value("string", "1.4.3"), value("string", "evil@1.4.3")],
                            [value("string", "1.4.4"), value("string", "evil@1.4.4")],
                        ],
                    ),
                ),
                httpx.Response(200, json=page(["path"], [])),
            ]
        )

        checked = await verify(client, self.plan_for("checkout-api", "evil@1.4.2"), compromise)

        assert checked.verdicts[0].to_version == "1.4.4"
