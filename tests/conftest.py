from __future__ import annotations

import orjson
import pytest

from firestop.config import Settings
from firestop.hydra.client import HydraClient

HTTP_URL = "http://hydra.test:8443"
QUERY_URL = f"{HTTP_URL}/v1/graphs/default/query"


@pytest.fixture
def settings() -> Settings:
    # Built explicitly rather than from the environment, so a developer's local
    # .env cannot change what the tests assert.
    return Settings(
        _env_file=None,
        hydradb_http_url=HTTP_URL,
        hydradb_admin_url="http://hydra.test:9090",
        hydradb_indexer_admin_url="http://hydra.test:9091",
        hydradb_auth_token="test-token-at-least-32-characters-long",
        hydradb_namespace="firestop",
        hydradb_graph_id="default",
        hydradb_cell_id="cell-0",
        write_batch_size=3,
    )


@pytest.fixture
async def client(settings: Settings):
    async with HydraClient(settings, max_retries=2) as hydra:
        yield hydra


def value(tag: str, raw: object = None) -> dict:
    """Build one of HydraDB's typed value envelopes."""
    return {"type": tag} if raw is None else {"type": tag, "value": raw}


def page(
    columns: list[str],
    rows: list[list[dict]],
    *,
    next_cursor: int | None = None,
    read_epoch: int = 41,
    bookmark: str | None = "bm-1",
) -> dict:
    return {
        "query_id": "http-query-1",
        "columns": columns,
        "rows": rows,
        "read_epoch": read_epoch,
        "next_cursor": next_cursor,
        "bookmark": bookmark,
    }


def ndjson(columns: list[str], rows: list[list[dict]]) -> str:
    """The streaming form: a header line, a line per row, then a summary."""
    lines = [{"type": "header", "columns": columns, "query_id": "http-query-1"}]
    lines += [{"type": "row", "values": row} for row in rows]
    lines.append({"type": "summary", "bookmark": "bm-1", "has_more": False})
    return "\n".join(orjson.dumps(line).decode() for line in lines)
