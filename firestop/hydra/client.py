"""Async HydraDB client over the typed JSON / NDJSON HTTP query API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

import httpx
import orjson

from firestop.config import Settings, get_settings
from firestop.hydra.errors import (
    HydraAuthError,
    HydraNotOwner,
    HydraQueryError,
    HydraUnavailable,
)
from firestop.hydra.values import decode_value

Consistency = Literal["causal", "strong"]

# Cap under 1 MiB so a fat row can't tip a full batch over.
_MAX_BODY_BYTES = 768 * 1024

_DEFAULT_PAGE_SIZE = 1024

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# resource_exhausted is not a transient 429 — asking again won't help.
_NOT_TRANSIENT = "resource_exhausted"


@dataclass(slots=True)
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    read_epoch: int | None = None
    bookmark: str | None = None

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def scalar(self, default: Any = None) -> Any:
        """First column of the first row, for single-value queries."""
        if not self.rows or not self.columns:
            return default
        return self.rows[0].get(self.columns[0], default)

    def column(self, name: str) -> list[Any]:
        return [row.get(name) for row in self.rows]


class HydraClient:
    """Talks OpenCypher to a HydraDB graph-node.

    One client owns one HTTP connection pool and is safe to share across
    concurrent tasks, which is what the crawler does.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout: float = 120.0,
        max_retries: int = 6,
    ) -> None:
        self._settings = settings or get_settings()
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self._settings.hydradb_http_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {self._settings.hydradb_auth_token}",
                "X-Graph-Namespace": self._settings.hydradb_namespace,
                "Content-Type": "application/json",
            },
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    async def __aenter__(self) -> HydraClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def _query_path(self) -> str:
        return f"/v1/graphs/{self._settings.hydradb_graph_id}/query"

    async def run(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        consistency: Consistency | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
        timeout_ms: int | None = None,
    ) -> QueryResult:
        """Execute one statement and collect every page of its result."""
        result = QueryResult()
        cursor: int | None = None
        # Every page of one result is the same request as far as the node is
        # concerned: a cursor is only valid against the id that produced it.
        request_id = _request_id()

        while True:
            body = self._body(query, parameters, consistency, page_size, timeout_ms, request_id)
            if cursor is not None:
                body["cursor"] = cursor

            payload = await self._post(body, query)
            if not result.columns:
                result.columns = list(payload.get("columns") or [])
            result.rows.extend(self._decode_rows(payload, result.columns))
            result.read_epoch = payload.get("read_epoch", result.read_epoch)
            result.bookmark = payload.get("bookmark") or result.bookmark

            cursor = payload.get("next_cursor")
            if cursor is None:
                return result

    async def stream(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        consistency: Consistency | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
        timeout_ms: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute one statement and yield rows as they arrive.

        Preferred over `run` for large traversals. Beyond avoiding the whole
        result in memory, the server holds one pinned snapshot for the life of
        an NDJSON stream, where paging with a cursor across separate requests
        does not carry that guarantee.
        """
        body = self._body(query, parameters, consistency, page_size, timeout_ms)
        columns: list[str] = []

        try:
            async with self._client.stream(
                "POST",
                self._query_path,
                content=orjson.dumps(body),
                headers={"Accept": "application/x-ndjson"},
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_response(response, query)

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    message = orjson.loads(line)
                    kind = message.get("type")

                    if kind == "header":
                        columns = list(message.get("columns") or [])
                    elif kind == "row":
                        values = [decode_value(v) for v in message.get("values") or []]
                        yield dict(zip(columns, values, strict=False))
                    elif kind == "error":
                        raise HydraQueryError(
                            message.get("code", "unknown"),
                            message.get("message", ""),
                            200,
                            query,
                        )
        except httpx.HTTPError as exc:
            raise HydraUnavailable(f"streaming query failed: {exc}") from exc

    async def write_batches(
        self,
        query: str,
        rows: Sequence[dict[str, Any]],
        *,
        parameter: str = "rows",
        extra_parameters: dict[str, Any] | None = None,
    ) -> int:
        """Run a batched `UNWIND` write, chunked to fit the node's body limit.

        Returns the number of rows submitted.
        """
        if not rows:
            return 0

        submitted = 0
        for chunk in _chunk_by_bytes(rows, self._settings.write_batch_size):
            parameters: dict[str, Any] = {parameter: list(chunk)}
            if extra_parameters:
                parameters.update(extra_parameters)
            await self.run(query, parameters)
            submitted += len(chunk)
        return submitted

    async def node_ready(self) -> bool:
        return await self._probe(f"{self._settings.hydradb_admin_url}/readyz")

    async def indexer_ready(self) -> bool:
        return await self._probe(f"{self._settings.hydradb_indexer_admin_url}/readyz")

    def _body(
        self,
        query: str,
        parameters: dict[str, Any] | None,
        consistency: Consistency | None,
        page_size: int,
        timeout_ms: int | None,
        query_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "cell_id": self._settings.hydradb_cell_id,
            "query": query,
            "page_size": page_size,
            "query_id": query_id or _request_id(),
        }
        if parameters:
            body["parameters"] = parameters
        if consistency:
            body["consistency"] = consistency
        if timeout_ms is not None:
            body["timeout_ms"] = timeout_ms
        return body

    @staticmethod
    def _decode_rows(payload: dict[str, Any], columns: list[str]) -> list[dict[str, Any]]:
        decoded = []
        for raw_row in payload.get("rows") or []:
            values = [decode_value(value) for value in raw_row]
            decoded.append(dict(zip(columns, values, strict=False)))
        return decoded

    async def _post(self, body: dict[str, Any], query: str) -> dict[str, Any]:
        content = orjson.dumps(body)
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                response = await self._client.post(self._query_path, content=content)
            except httpx.HTTPError as exc:
                # Several httpx errors stringify to nothing, so the class name is
                # the only clue about whether this was a timeout or a reset.
                detail = str(exc) or type(exc).__name__
                last_error = HydraUnavailable(
                    f"request failed after {attempt + 1} attempts: {detail}"
                )
                await asyncio.sleep(_backoff(attempt))
                continue

            retryable = response.status_code in _RETRY_STATUS and not _refused(response)
            if retryable and attempt < self._max_retries - 1:
                await asyncio.sleep(_backoff(attempt))
                continue

            if response.status_code >= 400:
                self._raise_for_response(response, query)

            return orjson.loads(response.content)

        raise last_error or HydraUnavailable("request failed after retries")

    @staticmethod
    def _raise_for_response(response: httpx.Response, query: str) -> None:
        code, message, owner = "unknown", response.text[:500], None
        try:
            error = orjson.loads(response.content).get("error") or {}
            code = error.get("code", code)
            message = error.get("message", message)
            owner = error.get("owner")
        except orjson.JSONDecodeError:
            pass

        status = response.status_code
        if status == 401 or status == 403:
            raise HydraAuthError(code, message, status, query)
        if status == 421:
            raise HydraNotOwner(code, message, status, owner)
        raise HydraQueryError(code, message, status, query)

    async def _probe(self, url: str) -> bool:
        try:
            response = await self._client.get(url, timeout=10.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200


def _refused(response: httpx.Response) -> bool:
    """Whether the node rejected the query itself rather than the timing."""
    try:
        error = orjson.loads(response.content).get("error") or {}
    except orjson.JSONDecodeError:
        return False
    return error.get("code") == _NOT_TRANSIENT


def _request_id() -> str:
    """Stable write idempotency key (node's auto ids reset on restart)."""
    return f"firestop-{uuid4()}"


def _backoff(attempt: int) -> float:
    """Exponential backoff, capped at 20s (ingest survives brief Docker blips)."""
    return min(0.25 * (2**attempt), 20.0)


def _chunk_by_bytes(
    rows: Sequence[dict[str, Any]], max_rows: int
) -> Iterable[list[dict[str, Any]]]:
    """Split rows into batches bounded by both row count and serialized size."""
    chunk: list[dict[str, Any]] = []
    chunk_bytes = 0

    for row in rows:
        row_bytes = len(orjson.dumps(row))
        too_many = len(chunk) >= max_rows
        too_big = chunk and chunk_bytes + row_bytes > _MAX_BODY_BYTES
        if too_many or too_big:
            yield chunk
            chunk, chunk_bytes = [], 0
        chunk.append(row)
        chunk_bytes += row_bytes

    if chunk:
        yield chunk
