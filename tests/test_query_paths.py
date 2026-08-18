from __future__ import annotations

import re

import httpx
import pytest
import respx

from firestop.hydra.client import HydraClient
from firestop.hydra.errors import HydraQueryError
from firestop.hydra.values import GraphPath
from firestop.query.paths import Endpoint, PathQuery, run
from tests.conftest import QUERY_URL, page
from tests.paths import service_path


def query(**overrides) -> PathQuery:
    defaults = {
        "source": Endpoint("Service", "name", ("checkout-api",)),
        "target": Endpoint("Release", "key", ("evil@1.4.2",)),
        "rel_types": ("USES_LOCKFILE", "PINS", "DEPENDS_ON"),
    }
    return PathQuery(**{**defaults, **overrides})


class TestGeneratedCypher:
    def test_the_config_map_is_inline_because_it_cannot_be_a_parameter(self):
        cypher = query().cypher()

        assert cypher.startswith("CALL algo.MSpaths({")
        assert "$" not in cypher

    def test_selectors_are_string_literals_not_ids(self):
        # The procedure matches a string property; a vertex id cannot select.
        assert "sourceValues: ['checkout-api']" in query().cypher()
        assert "targetValues: ['evil@1.4.2']" in query().cypher()

    def test_every_relationship_type_is_listed(self):
        # Variable-length MATCH can't cross rel types.
        cypher = query().cypher()

        assert "relTypes: ['USES_LOCKFILE', 'PINS', 'DEPENDS_ON']" in cypher

    def test_the_path_ceiling_is_always_stated(self):
        # It defaults to 1 server-side, which makes a blast radius look empty.
        cypher = query(path_count=250).cypher()

        assert "pathCount: 250" in cypher
        assert "maxLen: 8" in cypher

    def test_a_query_without_targets_omits_them_entirely(self):
        cypher = query(target=None).cypher()

        assert "targetValues" not in cypher
        assert "sourceValues" in cypher

    def test_direction_can_be_reversed(self):
        assert "relDirection: 'incoming'" in query(direction="incoming").cypher()

    def test_a_quote_in_a_selector_cannot_end_the_literal(self):
        # npm names cannot contain one, but service names come from a manifest.
        cypher = query(source=Endpoint("Service", "name", ("it's-fine",))).cypher()

        assert r"'it\'s-fine'" in cypher

    def test_a_backslash_in_a_selector_is_escaped(self):
        cypher = query(source=Endpoint("Service", "name", ("back\\slash",))).cypher()

        assert r"'back\\slash'" in cypher


@respx.mock
async def test_run_decodes_the_returned_paths(client: HydraClient):
    respx.post(QUERY_URL).respond(
        json=page(["path"], [[service_path("checkout-api", package="evil", version="1.4.2")]])
    )

    found = await run(client, query())

    assert len(found) == 1
    assert isinstance(found.paths[0], GraphPath)
    assert found.paths[0].source.get("name") == "checkout-api"


@respx.mock
async def test_a_long_selector_list_is_split_across_requests(client: HydraClient):
    # Whole query is one body; stay under the node cap.
    route = respx.post(QUERY_URL).respond(json=page(["path"], []))
    services = tuple(f"service-{index:05d}-{'x' * 200}" for index in range(4000))

    await run(client, query(source=Endpoint("Service", "name", services)))

    assert len(route.calls) > 1
    sent = [
        re.findall(r"sourceValues: \[(.*?)\]", call.request.content.decode())[0]
        for call in route.calls
    ]
    assert sum(chunk.count("service-") for chunk in sent) == len(services)


@respx.mock
async def test_an_empty_selector_list_still_asks_once(client: HydraClient):
    # Unknown selectors yield empty, not an error.
    route = respx.post(QUERY_URL).respond(json=page(["path"], []))

    found = await run(client, query(source=Endpoint("Service", "name", ())))

    assert found.paths == []
    assert len(route.calls) == 1


def refused() -> httpx.Response:
    """What the node returns when the frontier would exceed admission control."""
    return httpx.Response(
        429,
        json={
            "error": {
                "code": "resource_exhausted",
                "message": "native_path_frontier_paths rejected by admission control",
            }
        },
    )


class TestNegotiatingDepth:
    # Hub packages: node refuses with resource_exhausted; negotiate depth down.

    @respx.mock
    async def test_a_refused_frontier_is_retried_one_hop_shallower(self, client: HydraClient):
        route = respx.post(QUERY_URL).mock(
            side_effect=[refused(), httpx.Response(200, json=page(["path"], []))]
        )

        found = await run(client, query(max_len=8))

        assert len(route.calls) == 2
        assert found.depth == 7
        assert found.shortened

    @respx.mock
    async def test_the_depth_that_worked_is_reported(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[refused(), refused(), httpx.Response(200, json=page(["path"], []))]
        )

        found = await run(client, query(max_len=8))

        assert found.depth == 6
        assert found.asked_for == 8

    @respx.mock
    async def test_an_answer_at_the_asked_depth_is_not_marked_short(self, client: HydraClient):
        respx.post(QUERY_URL).respond(json=page(["path"], []))

        found = await run(client, query(max_len=8))

        assert found.depth == 8
        assert not found.shortened

    @respx.mock
    async def test_the_descent_stops_rather_than_asking_a_useless_question(
        self, client: HydraClient
    ):
        # Under three hops a service cannot even reach its own transitive
        # dependencies, so there is nothing left worth answering.
        route = respx.post(QUERY_URL).mock(side_effect=[refused()] * 12)

        with pytest.raises(HydraQueryError):
            await run(client, query(max_len=8))

        assert len(route.calls) == 6

    @respx.mock
    async def test_later_chunks_start_from_the_depth_that_worked(self, client: HydraClient):
        # Otherwise one hub package costs a fresh descent for every chunk.
        route = respx.post(QUERY_URL).mock(
            side_effect=[refused(), httpx.Response(200, json=page(["path"], []))]
            + [httpx.Response(200, json=page(["path"], []))] * 8
        )
        services = tuple(f"service-{index:05d}-{'x' * 200}" for index in range(4000))

        found = await run(client, query(source=Endpoint("Service", "name", services), max_len=8))

        assert found.depth == 7
        assert all("maxLen: 7" in call.request.content.decode() for call in route.calls[1:])
