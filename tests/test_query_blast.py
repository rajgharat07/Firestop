from __future__ import annotations

import httpx
import respx

from firestop.hydra.client import HydraClient
from firestop.query.blast import exposure, reach
from firestop.query.compromise import Compromise
from firestop.schema.model import OPEN_INTERVAL_END
from tests.conftest import QUERY_URL, page, value
from tests.paths import service_path

JANUARY = 1_640_995_200  # 2022-01-01
JUNE = 1_654_041_600  # 2022-06-01
DECEMBER = 1_672_531_200  # 2023-01-01

COMPROMISE = Compromise(
    packages=("evil",),
    keys=("evil@1.4.2",),
    advisory="GHSA-test",
    severity="critical",
)


def services(*names: tuple[str, str]) -> dict:
    return page(
        ["name", "criticality"],
        [[value("string", name), value("string", tier)] for name, tier in names],
    )


def responses(service_rows: dict, *paths: dict) -> list[httpx.Response]:
    return [
        httpx.Response(200, json=service_rows),
        httpx.Response(200, json=page(["path"], [[found] for found in paths])),
    ]


@respx.mock
async def test_a_service_reaching_the_bad_release_is_reported(client: HydraClient):
    respx.post(QUERY_URL).mock(
        side_effect=responses(
            services(("checkout-api", "tier-1")),
            service_path("checkout-api", package="evil", version="1.4.2"),
        )
    )

    radius = await exposure(client, COMPROMISE)

    assert radius.exposed == 1
    assert radius.services[0].service == "checkout-api"
    assert radius.services[0].criticality == "tier-1"


@respx.mock
async def test_every_service_is_evaluated_in_one_traversal(client: HydraClient):
    # One multi-source call, not N×M.
    route = respx.post(QUERY_URL)
    route.mock(
        side_effect=responses(
            services(("checkout-api", "tier-1"), ("batch-jobs", "tier-2")),
            service_path("checkout-api", package="evil", version="1.4.2"),
            service_path("batch-jobs", package="evil", version="1.4.2"),
        )
    )

    radius = await exposure(client, COMPROMISE)

    assert radius.exposed == 2
    assert len(route.calls) == 2  # one for the service list, one for the traversal


@respx.mock
async def test_a_service_that_does_not_reach_it_is_absent(client: HydraClient):
    respx.post(QUERY_URL).mock(
        side_effect=responses(
            services(("checkout-api", "tier-1"), ("docs-site", "tier-3")),
            service_path("checkout-api", package="evil", version="1.4.2"),
        )
    )

    radius = await exposure(client, COMPROMISE)

    assert [found.service for found in radius.services] == ["checkout-api"]
    assert not radius.clean


@respx.mock
async def test_nothing_exposed_reads_as_clean(client: HydraClient):
    respx.post(QUERY_URL).mock(side_effect=responses(services(("docs-site", "tier-3"))))

    radius = await exposure(client, COMPROMISE)

    assert radius.clean
    assert radius.exposed == 0


@respx.mock
async def test_services_are_ordered_by_how_close_the_exposure_is(client: HydraClient):
    respx.post(QUERY_URL).mock(
        side_effect=responses(
            services(("checkout-api", "tier-1"), ("batch-jobs", "tier-2")),
            service_path(
                "checkout-api",
                package="evil",
                version="1.4.2",
                hops=[("express", "4.17.1"), ("body-parser", "1.19.0")],
            ),
            service_path("batch-jobs", package="evil", version="1.4.2"),
        )
    )

    radius = await exposure(client, COMPROMISE)

    assert [found.service for found in radius.services] == ["batch-jobs", "checkout-api"]
    assert radius.services[0].shortest < radius.services[1].shortest


@respx.mock
async def test_the_chain_explains_how_the_release_got_there(client: HydraClient):
    respx.post(QUERY_URL).mock(
        side_effect=responses(
            services(("checkout-api", "tier-1")),
            service_path(
                "checkout-api",
                package="evil",
                version="1.4.2",
                hops=[("express", "4.17.1")],
            ),
        )
    )

    radius = await exposure(client, COMPROMISE)
    chain = radius.services[0].paths[0].chain

    assert chain[0] == "checkout-api"
    assert "express@4.17.1" in chain
    assert chain[-1] == "evil@1.4.2"


@respx.mock
async def test_a_declared_dependency_is_marked_direct(client: HydraClient):
    # direct pin vs inherited.
    respx.post(QUERY_URL).mock(
        side_effect=responses(
            services(("checkout-api", "tier-1")),
            service_path("checkout-api", package="evil", version="1.4.2", direct=True),
        )
    )

    radius = await exposure(client, COMPROMISE)

    assert radius.services[0].direct is True


@respx.mock
async def test_an_inherited_dependency_is_not_direct(client: HydraClient):
    respx.post(QUERY_URL).mock(
        side_effect=responses(
            services(("checkout-api", "tier-1")),
            service_path(
                "checkout-api",
                package="evil",
                version="1.4.2",
                hops=[("express", "4.17.1")],
                direct=True,
            ),
        )
    )

    radius = await exposure(client, COMPROMISE)

    assert radius.services[0].direct is False


@respx.mock
async def test_a_build_time_path_is_separated_from_a_shipping_one(client: HydraClient):
    respx.post(QUERY_URL).mock(
        side_effect=responses(
            services(("checkout-api", "tier-1")),
            service_path(
                "checkout-api",
                package="evil",
                version="1.4.2",
                hops=[("jest", "27.5.1")],
                dev=True,
            ),
        )
    )

    radius = await exposure(client, COMPROMISE)

    assert radius.services[0].runtime is False
    assert radius.services[0].paths[0].build_time is True


class TestAsOf:
    @respx.mock
    async def test_a_path_live_at_the_moment_asked_about_counts(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=responses(
                services(("checkout-api", "tier-1")),
                service_path(
                    "checkout-api",
                    package="evil",
                    version="1.4.2",
                    hops=[("express", "4.17.1")],
                    valid_from=JANUARY,
                    valid_to=DECEMBER,
                ),
            )
        )

        radius = await exposure(client, COMPROMISE, as_of=JUNE)

        assert radius.exposed == 1

    @respx.mock
    async def test_a_path_that_had_already_closed_does_not(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=responses(
                services(("checkout-api", "tier-1")),
                service_path(
                    "checkout-api",
                    package="evil",
                    version="1.4.2",
                    hops=[("express", "4.17.1")],
                    valid_from=JANUARY,
                    valid_to=JUNE,
                ),
            )
        )

        radius = await exposure(client, COMPROMISE, as_of=DECEMBER)

        assert radius.clean
        assert radius.paths_returned == 1  # the traversal found it; the interval ruled it out

    @respx.mock
    async def test_a_path_that_had_not_opened_yet_does_not(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=responses(
                services(("checkout-api", "tier-1")),
                service_path(
                    "checkout-api",
                    package="evil",
                    version="1.4.2",
                    hops=[("express", "4.17.1")],
                    valid_from=JUNE,
                    valid_to=OPEN_INTERVAL_END,
                ),
            )
        )

        radius = await exposure(client, COMPROMISE, as_of=JANUARY)

        assert radius.clean

    @respx.mock
    async def test_without_a_moment_every_coherent_path_counts(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=responses(
                services(("checkout-api", "tier-1")),
                service_path(
                    "checkout-api",
                    package="evil",
                    version="1.4.2",
                    hops=[("express", "4.17.1")],
                    valid_from=JANUARY,
                    valid_to=JUNE,
                ),
            )
        )

        radius = await exposure(client, COMPROMISE)

        assert radius.exposed == 1


@respx.mock
async def test_reach_counts_the_packages_downstream_of_a_compromise(client: HydraClient):
    respx.post(QUERY_URL).respond(
        json=page(
            ["path"],
            [
                [
                    service_path(
                        "checkout-api",
                        package="evil",
                        version="1.4.2",
                        hops=[("express", "4.17.1")],
                    )
                ]
            ],
        )
    )

    spread = await reach(client, COMPROMISE)

    assert "express" in spread.packages
    # Package does not expose itself.
    assert "evil" not in spread.packages


@respx.mock
async def test_a_truncated_traversal_says_so(client: HydraClient):
    # Truncation must be reported.
    respx.post(QUERY_URL).mock(
        side_effect=responses(
            services(("checkout-api", "tier-1")),
            service_path("checkout-api", package="evil", version="1.4.2"),
        )
    )

    radius = await exposure(client, COMPROMISE, path_count=1)

    assert radius.truncated is True
