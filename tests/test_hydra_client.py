from __future__ import annotations

import httpx
import orjson
import pytest
import respx

from firestop.hydra.client import HydraClient
from firestop.hydra.errors import (
    HydraAuthError,
    HydraNotOwner,
    HydraQueryError,
    HydraUnavailable,
)
from tests.conftest import QUERY_URL, page, value


def bodies(route: respx.Route) -> list[dict]:
    return [orjson.loads(call.request.content) for call in route.calls]


@respx.mock
async def test_run_decodes_typed_values(client: HydraClient):
    respx.post(QUERY_URL).respond(
        json=page(
            ["name", "count", "ratio", "ok", "missing", "vid"],
            [
                [
                    value("string", "lodash"),
                    value("integer", 42),
                    value("float", 1.5),
                    value("boolean", True),
                    value("null"),
                    value("vertex_id", 99),
                ]
            ],
        )
    )

    result = await client.run("MATCH (n:Package) RETURN n.name AS name")

    assert result.rows == [
        {
            "name": "lodash",
            "count": 42,
            "ratio": 1.5,
            "ok": True,
            "missing": None,
            "vid": 99,
        }
    ]
    assert result.read_epoch == 41
    assert result.bookmark == "bm-1"


@respx.mock
async def test_run_sends_cell_id_and_namespace(client: HydraClient):
    route = respx.post(QUERY_URL).respond(json=page(["total"], []))

    await client.run("MATCH (n:Package) RETURN count(*) AS total")

    request = route.calls[0].request
    assert request.headers["x-graph-namespace"] == "firestop"
    assert request.headers["authorization"].startswith("Bearer ")
    assert bodies(route)[0]["cell_id"] == "cell-0"


@respx.mock
async def test_run_omits_parameters_when_there_are_none(client: HydraClient):
    route = respx.post(QUERY_URL).respond(json=page(["total"], []))

    await client.run("MATCH (n:Package) RETURN count(*) AS total")

    assert "parameters" not in bodies(route)[0]


@respx.mock
async def test_run_never_sends_read_epoch(client: HydraClient):
    # Do not echo read_epoch on HTTP.
    route = respx.post(QUERY_URL).respond(json=page(["total"], []))

    await client.run("MATCH (n:Package) RETURN count(*) AS total")

    assert "read_epoch" not in bodies(route)[0]


@respx.mock
async def test_run_follows_the_cursor_until_it_is_exhausted(client: HydraClient):
    route = respx.post(QUERY_URL)
    route.side_effect = [
        httpx.Response(200, json=page(["id"], [[value("integer", 1)]], next_cursor=7)),
        httpx.Response(200, json=page(["id"], [[value("integer", 2)]], next_cursor=8)),
        httpx.Response(200, json=page(["id"], [[value("integer", 3)]])),
    ]

    result = await client.run("MATCH (n:Package) RETURN n.id AS id")

    assert result.column("id") == [1, 2, 3]
    sent = bodies(route)
    assert "cursor" not in sent[0]
    assert [body["cursor"] for body in sent[1:]] == [7, 8]


@respx.mock
async def test_consistency_is_forwarded_only_when_requested(client: HydraClient):
    route = respx.post(QUERY_URL).respond(json=page(["id"], []))

    await client.run("MATCH (n {id: 1}) RETURN n.id AS id")
    await client.run("MATCH (n {id: 1}) RETURN n.id AS id", consistency="strong")

    sent = bodies(route)
    assert "consistency" not in sent[0]
    assert sent[1]["consistency"] == "strong"


@respx.mock
async def test_parse_rejection_becomes_a_query_error(client: HydraClient):
    respx.post(QUERY_URL).respond(
        status_code=400,
        json={
            "error": {
                "code": "unsupported_query",
                "message": "WHERE supports boolean combinations of property comparisons",
            }
        },
    )

    with pytest.raises(HydraQueryError) as caught:
        await client.run("MATCH (n:Package) WHERE n.name IN ['a'] RETURN n.id AS id")

    assert caught.value.code == "unsupported_query"
    assert caught.value.status == 400
    # Error carries the rejected statement.
    assert "IN" in caught.value.query


@respx.mock
async def test_unauthenticated_becomes_an_auth_error(client: HydraClient):
    respx.post(QUERY_URL).respond(
        status_code=401,
        json={"error": {"code": "unauthenticated", "message": "bearer required"}},
    )

    with pytest.raises(HydraAuthError):
        await client.run("MATCH (n {id: 1}) RETURN n.id AS id")


@respx.mock
async def test_wrong_owner_carries_the_owning_node(client: HydraClient):
    respx.post(QUERY_URL).respond(
        status_code=421,
        json={
            "error": {
                "code": "not_owner",
                "message": "cell is owned elsewhere",
                "owner": "node-3",
            }
        },
    )

    with pytest.raises(HydraNotOwner) as caught:
        await client.run("MATCH (n {id: 1}) RETURN n.id AS id")

    assert caught.value.owner == "node-3"


@respx.mock
async def test_non_json_error_body_still_raises(client: HydraClient):
    respx.post(QUERY_URL).respond(status_code=500, text="upstream exploded")

    with pytest.raises(HydraQueryError) as caught:
        await client.run("MATCH (n {id: 1}) RETURN n.id AS id")

    assert "upstream exploded" in caught.value.message


@respx.mock
async def test_every_query_carries_its_own_id(client: HydraClient):
    # Client-supplied query_id for write idempotency across restarts.
    respx.post(QUERY_URL).respond(json=page([], []))

    await client.run("MATCH (n:Package) RETURN n.id AS id")
    await client.run("MATCH (n:Release) RETURN n.id AS id")

    ids = [orjson.loads(call.request.content)["query_id"] for call in respx.calls]
    assert all(request_id.startswith("firestop-") for request_id in ids)
    assert len(set(ids)) == 2


@respx.mock
async def test_paging_stays_within_one_request_id(client: HydraClient):
    # Same query_id required across pages.
    route = respx.post(QUERY_URL)
    route.side_effect = [
        httpx.Response(200, json=page(["id"], [[value("integer", 1)]], next_cursor=7)),
        httpx.Response(200, json=page(["id"], [[value("integer", 2)]])),
    ]

    await client.run("MATCH (n:Package) RETURN n.id AS id")

    sent = bodies(route)
    assert sent[1]["cursor"] == 7
    assert sent[0]["query_id"] == sent[1]["query_id"]


@respx.mock
async def test_a_retry_reuses_the_request_id(client: HydraClient):
    # Retry replays the stored result.
    route = respx.post(QUERY_URL)
    route.side_effect = [
        httpx.Response(503, json={"error": {"code": "unavailable", "message": "warming"}}),
        httpx.Response(200, json=page([], [])),
    ]

    await client.run("MATCH (n {id: 1}) RETURN n.id AS id")

    sent = bodies(route)
    assert sent[0]["query_id"] == sent[1]["query_id"]


@respx.mock
async def test_transient_status_is_retried(client: HydraClient):
    route = respx.post(QUERY_URL)
    route.side_effect = [
        httpx.Response(503, json={"error": {"code": "unavailable", "message": "warming"}}),
        httpx.Response(200, json=page(["id"], [[value("integer", 5)]])),
    ]

    result = await client.run("MATCH (n {id: 1}) RETURN n.id AS id")

    assert result.scalar() == 5
    assert len(route.calls) == 2


@respx.mock
async def test_connect_failure_raises_unavailable(client: HydraClient):
    respx.post(QUERY_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(HydraUnavailable):
        await client.run("MATCH (n {id: 1}) RETURN n.id AS id")


@respx.mock
async def test_write_batches_split_on_row_count(client: HydraClient):
    route = respx.post(QUERY_URL).respond(json=page([], []))
    rows = [{"id": index, "name": f"pkg-{index}"} for index in range(7)]

    submitted = await client.write_batches(
        "UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Package, n.name = row.name",
        rows,
    )

    # Fixture write_batch_size=3 → chunks of 3+3+1.
    assert submitted == 7
    sent = [len(body["parameters"]["rows"]) for body in bodies(route)]
    assert sent == [3, 3, 1]


@respx.mock
async def test_write_batches_split_on_serialized_size(client: HydraClient):
    route = respx.post(QUERY_URL).respond(json=page([], []))
    # Oversized rows force a split before the row-count limit.
    rows = [{"id": index, "blob": "x" * 500_000} for index in range(2)]

    await client.write_batches("UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Package", rows)

    assert [len(body["parameters"]["rows"]) for body in bodies(route)] == [1, 1]


@respx.mock
async def test_write_batches_passes_extra_parameters(client: HydraClient):
    route = respx.post(QUERY_URL).respond(json=page([], []))

    await client.write_batches(
        "UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Package, n.at = $at",
        [{"id": 1}],
        extra_parameters={"at": 1_700_000_000},
    )

    assert bodies(route)[0]["parameters"]["at"] == 1_700_000_000


@respx.mock
async def test_empty_write_is_not_a_request(client: HydraClient):
    route = respx.post(QUERY_URL).respond(json=page([], []))

    assert await client.write_batches("UNWIND $rows AS row MERGE (n {id: row.id})", []) == 0
    assert not route.calls


@respx.mock
async def test_stream_yields_rows_from_ndjson(client: HydraClient):
    lines = "\n".join(
        [
            orjson.dumps({"type": "header", "columns": ["name"], "query_id": "q"}).decode(),
            orjson.dumps({"type": "row", "values": [value("string", "lodash")]}).decode(),
            orjson.dumps({"type": "row", "values": [value("string", "react")]}).decode(),
            orjson.dumps({"type": "summary", "bookmark": "bm", "has_more": False}).decode(),
        ]
    )
    route = respx.post(QUERY_URL).respond(
        text=lines, headers={"Content-Type": "application/x-ndjson"}
    )

    rows = [row async for row in client.stream("MATCH (n:Package) RETURN n.name AS name")]

    assert rows == [{"name": "lodash"}, {"name": "react"}]
    assert route.calls[0].request.headers["accept"] == "application/x-ndjson"


@respx.mock
async def test_stream_raises_on_an_error_line(client: HydraClient):
    error = {"type": "error", "code": "query_timeout", "message": "too slow"}
    lines = "\n".join(
        [
            orjson.dumps({"type": "header", "columns": ["name"], "query_id": "q"}).decode(),
            orjson.dumps(error).decode(),
        ]
    )
    respx.post(QUERY_URL).respond(text=lines)

    with pytest.raises(HydraQueryError) as caught:
        async for _ in client.stream("MATCH (n:Package) RETURN n.name AS name"):
            pass

    assert caught.value.code == "query_timeout"
